import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from yt_dlp import YoutubeDL


# =========================================================
# CONFIG & PATHS
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

INITIAL_ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_DIR = Path(__file__).resolve().parent
LOCAL_NODE_BIN = BASE_DIR / ".node" / "bin"
BGUTIL_DIR = BASE_DIR / "bgutil-ytdlp-pot-provider" / "server"
BGUTIL_MAIN = BGUTIL_DIR / "build" / "main.js"
BGUTIL_PORT = int(os.getenv("BGUTIL_PORT", "4416"))
BGUTIL_PROCESS = None

if LOCAL_NODE_BIN.is_dir():
    os.environ["PATH"] = str(LOCAL_NODE_BIN) + os.pathsep + os.environ.get("PATH", "")

MAX_FILE_SIZE = 49 * 1024 * 1024
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()
DB_FILE = BASE_DIR / "bot_database.db"

DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)


# =========================================================
# DATABASE SYSTEM (SQLite / PostgreSQL)
# =========================================================

def get_db_connection():
    if DATABASE_URL:
        import psycopg2
        db_url = DATABASE_URL
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(db_url), True
    else:
        conn = sqlite3.connect(DB_FILE)
        return conn, False

def execute_query(query: str, params: tuple = (), fetchone=False, fetchall=False, commit=False):
    conn, is_postgres = get_db_connection()
    try:
        cursor = conn.cursor()
        sql = query
        if is_postgres:
            sql = sql.replace("?", "%s").replace("excluded.", "EXCLUDED.")
        cursor.execute(sql, params)
        
        res = None
        if fetchone:
            res = cursor.fetchone()
        elif fetchall:
            res = cursor.fetchall()
            
        if commit:
            conn.commit()
        return res
    finally:
        conn.close()

def init_db():
    execute_query("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            lang TEXT
        )
    """, commit=True)
    
    execute_query("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        )
    """, commit=True)
    
    execute_query("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            invite_link TEXT
        )
    """, commit=True)
    
    if INITIAL_ADMIN_ID > 0:
        execute_query("""
            INSERT INTO admins (user_id) VALUES (?)
            ON CONFLICT (user_id) DO NOTHING
        """, (INITIAL_ADMIN_ID,), commit=True)

def get_user_lang(user_id: int) -> str | None:
    row = execute_query("SELECT lang FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if row and row[0]:
        return row[0]
    return None

def set_user_lang(user_id: int, lang: str):
    execute_query("""
        INSERT INTO users (user_id, lang) VALUES (?, ?)
        ON CONFLICT (user_id) DO UPDATE SET lang = excluded.lang
    """, (user_id, lang), commit=True)

def is_admin(user_id: int) -> bool:
    if user_id == INITIAL_ADMIN_ID:
        return True
    row = execute_query("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    return row is not None

def add_admin(user_id: int):
    execute_query("""
        INSERT INTO admins (user_id) VALUES (?)
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id,), commit=True)

def get_sponsored_channels():
    rows = execute_query("SELECT channel_id, title, invite_link FROM channels", fetchall=True)
    if not rows:
        return []
    return [{"id": r[0], "title": r[1], "link": r[2]} for r in rows]

def get_sponsored_channel(channel_id: str):
    row = execute_query("SELECT channel_id, title, invite_link FROM channels WHERE channel_id = ?", (channel_id,), fetchone=True)
    if row:
        return {"id": row[0], "title": row[1], "link": row[2]}
    return None

def add_sponsored_channel(channel_id: str, title: str, link: str):
    execute_query("""
        INSERT INTO channels (channel_id, title, invite_link) VALUES (?, ?, ?)
        ON CONFLICT (channel_id) DO UPDATE SET title = excluded.title, invite_link = excluded.invite_link
    """, (channel_id, title, link), commit=True)

def update_sponsored_channel(channel_id: str, title: str, link: str):
    execute_query("""
        UPDATE channels SET title = ?, invite_link = ? WHERE channel_id = ?
    """, (title, link, channel_id), commit=True)

def delete_sponsored_channel(channel_id: str):
    execute_query("DELETE FROM channels WHERE channel_id = ?", (channel_id,), commit=True)


# =========================================================
# LOCALIZATION (TEXTS)
# =========================================================

TEXTS = {
    "ua": {
        "select_lang_prompt": "👋 Вітаємо! Будь ласка, оберіть мову спілкування:",
        "start": "Привіт! 👋\nНадішли посилання на YouTube або YouTube Music.\nМожна відео, трек або плейлист.",
        "choose_format": "Обери формат:",
        "audio_btn": "🎵 Аудіо",
        "video_btn": "🎬 Відео",
        "download_audio": "🎵 Завантажити аудіо",
        "settings": "⚙️ Налаштування\nПоточна мова: Українська 🇺🇦",
        "change_lang": "🌐 Змінити мову",
        "lang_set": "✅ Мову успішно змінено на Українську 🇺🇦",
        "sub_required": "⚠️ **Для використання бота підпишіться на наші канали-спонсори:**",
        "check_sub_btn": "🔄 Перевірити підписку",
        "sub_success": "✅ Дякуємо за підписку! Надішліть посилання ще раз.",
        "sub_failed": "❌ Ви підписалися не на всі канали!",
        "invalid_url": "❌ Надішли коректне посилання YouTube або YouTube Music.",
        "back_btn": "🔙 Назад",
        "close_btn": "❌ Закрити",
        "fetching_qualities": "🔎 Отримую список доступних якостей...",
        "no_qualities": "❌ Не вдалося отримати варіанти якості для цього відео.",
        "choose_quality": "📹 **{title}**\n\nОберіть бажану якість:",
        "downloading_video": "⏳ Завантажую відео у якості {height}p...",
        "downloading_audio": "⏳ Завантажую аудіо...",
        "file_too_large_video": "📦 **Файл перевищує 50 МБ** ({mb} МБ).\nTelegram не дозволяє надсилати такі файли напряму.\n\n🔗 [Натисніть сюди, щоб завантажити відео]({link})",
        "file_too_large_audio": "📦 **Аудіо перевищує 50 МБ** ({mb} МБ).\n\n🔗 [Натисніть сюди, щоб завантажити аудіо]({link})",
        "link_lost": "❌ Посилання втрачено. Надішли його ще раз.",
        "admin_title": "🔑 **Панель адміністратора**",
        "admin_add_admin_btn": "➕ Додати адміна",
        "admin_add_channel_btn": "📢 Додати спонсора",
        "admin_list_channels_btn": "📋 Список спонсорів",
        "admin_enter_admin_id": "Надішліть Telegram ID користувача, якому хочете надати права адміна:",
        "admin_enter_channel_data": "Надішліть дані каналу у такому форматі (через пробіл):\n`@channel_id Назва_Каналу https://t.me/link`\n\n⚠️ **Бот повинен бути доданий в цей канал як АДМІНІСТРАТОР!**",
        "admin_channel_added": "✅ Канал `{title}` додано до списку спонсорів!",
        "admin_invalid_channel_format": "❌ Невірний формат. Введіть: `@channel_id Назва Посилання`",
        "admin_admin_added": "✅ Користувача `{id}` успішно додано до адмінів!",
        "admin_invalid_id": "❌ Введіть числовий Telegram ID.",
        "sponsors_empty": "📋 **Спонсорських каналів немає.**",
        "sponsors_list_title": "📋 **Керування спонсорськими каналами:**\nОберіть канал для керування:",
        "sponsor_info": "📢 **Канал:** {title}\n🆔 **ID:** `{id}`\n🔗 **Посилання:** {link}",
        "edit_btn": "✏️ Редагувати",
        "delete_btn": "🗑 Видалити",
        "sponsor_deleted": "✅ Канал успішно видалено!",
        "edit_sponsor_prompt": "Надішліть нову назву та посилання через пробіл:\n`Нова_Назва https://t.me/link`",
        "sponsor_updated": "✅ Дані каналу оновлено!",
    },
    "en": {
        "select_lang_prompt": "👋 Welcome! Please choose your preferred language:",
        "start": "Hello! 👋\nSend a YouTube or YouTube Music link.\nVideo, track, or playlist supported.",
        "choose_format": "Choose format:",
        "audio_btn": "🎵 Audio",
        "video_btn": "🎬 Video",
        "download_audio": "🎵 Download audio",
        "settings": "⚙️ Settings\nCurrent language: English 🇬🇧",
        "change_lang": "🌐 Change language",
        "lang_set": "✅ Language successfully set to English 🇬🇧",
        "sub_required": "⚠️ **Please subscribe to our sponsor channels to use the bot:**",
        "check_sub_btn": "🔄 Check subscription",
        "sub_success": "✅ Thank you for subscribing! Please send the link again.",
        "sub_failed": "❌ You have not subscribed to all channels!",
        "invalid_url": "❌ Please send a valid YouTube or YouTube Music link.",
        "back_btn": "🔙 Back",
        "close_btn": "❌ Close",
        "fetching_qualities": "🔎 Fetching available video qualities...",
        "no_qualities": "❌ Could not get quality options for this video.",
        "choose_quality": "📹 **{title}**\n\nChoose desired quality:",
        "downloading_video": "⏳ Downloading video in {height}p quality...",
        "downloading_audio": "⏳ Downloading audio...",
        "file_too_large_video": "📦 **File exceeds 50 MB** ({mb} MB).\nTelegram doesn't allow direct sending of large files.\n\n🔗 [Click here to download video]({link})",
        "file_too_large_audio": "📦 **Audio exceeds 50 MB** ({mb} MB).\n\n🔗 [Click here to download audio]({link})",
        "link_lost": "❌ Link lost. Please send it again.",
        "admin_title": "🔑 **Admin Panel**",
        "admin_add_admin_btn": "➕ Add Admin",
        "admin_add_channel_btn": "📢 Add Sponsor",
        "admin_list_channels_btn": "📋 Sponsor List",
        "admin_enter_admin_id": "Send the Telegram ID of the user you want to promote to admin:",
        "admin_enter_channel_data": "Send channel details in this format (space separated):\n`@channel_id Channel_Name https://t.me/link`\n\n⚠️ **Bot must be an ADMIN in this channel!**",
        "admin_channel_added": "✅ Channel `{title}` added to sponsors!",
        "admin_invalid_channel_format": "❌ Invalid format. Enter: `@channel_id Name Link`",
        "admin_admin_added": "✅ User `{id}` added as admin!",
        "admin_invalid_id": "❌ Please enter a numeric Telegram ID.",
        "sponsors_empty": "📋 **No sponsor channels found.**",
        "sponsors_list_title": "📋 **Sponsor Channel Management:**\nSelect a channel to manage:",
        "sponsor_info": "📢 **Channel:** {title}\n🆔 **ID:** `{id}`\n🔗 **Link:** {link}",
        "edit_btn": "✏️ Edit",
        "delete_btn": "🗑 Delete",
        "sponsor_deleted": "✅ Channel successfully deleted!",
        "edit_sponsor_prompt": "Send new name and link space-separated:\n`New_Name https://t.me/link`",
        "sponsor_updated": "✅ Channel details updated!",
    }
}

def get_text(lang: str, key: str) -> str:
    l = lang if lang in TEXTS else "ua"
    return TEXTS[l].get(key, TEXTS["ua"].get(key, key))


# =========================================================
# HELPER: FIRST-RUN LANGUAGE CHECK
# =========================================================

def get_language_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇺🇦 Українська", callback_data="set_lang:ua"),
            InlineKeyboardButton("🇬🇧 English", callback_data="set_lang:en")
        ]
    ])

async def check_user_language_or_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    
    if lang is None:
        text = "👋 Вітаємо! Будь ласка, оберіть мову:\n👋 Welcome! Please choose a language:"
        if update.callback_query:
            await update.callback_query.message.reply_text(text, reply_markup=get_language_selection_keyboard())
        elif update.message:
            await update.message.reply_text(text, reply_markup=get_language_selection_keyboard())
        return None
        
    return lang


# =========================================================
# SPONSOR SUB CHECKER
# =========================================================

async def check_user_subscriptions(bot, user_id: int):
    channels = get_sponsored_channels()
    unsubscribed = []
    
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if member.status not in ["creator", "administrator", "member"]:
                unsubscribed.append(ch)
        except Exception as e:
            logger.warning("Could not check membership for channel %s: %s", ch["id"], e)
            unsubscribed.append(ch)
            
    return unsubscribed


# =========================================================
# YT-DLP CORE LOGIC
# =========================================================

def youtube_options_base():
    options = {
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {"player_client": ["android_vr", "mweb"]},
            "youtubepot-bgutilhttp": {"base_url": f"http://127.0.0.1:{BGUTIL_PORT}"},
        },
        "js_runtimes": {
            "node": {"path": str(LOCAL_NODE_BIN / "node")}
        },
    }

    if YOUTUBE_COOKIES:
        cookie_path = Path(YOUTUBE_COOKIES)
        if cookie_path.is_file():
            options["cookiefile"] = str(cookie_path)

    return options


def start_bgutil_provider():
    global BGUTIL_PROCESS
    if not BGUTIL_MAIN.is_file():
        return False
    node = LOCAL_NODE_BIN / "node"
    if not node.is_file():
        return False

    BGUTIL_PROCESS = subprocess.Popen(
        [str(node), str(BGUTIL_MAIN), "--port", str(BGUTIL_PORT)],
        cwd=str(BGUTIL_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )

    import urllib.request
    deadline = time.time() + 15
    while time.time() < deadline:
        if BGUTIL_PROCESS.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{BGUTIL_PORT}/ping", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def stop_bgutil_provider():
    global BGUTIL_PROCESS
    if BGUTIL_PROCESS is not None and BGUTIL_PROCESS.poll() is None:
        BGUTIL_PROCESS.terminate()
        try:
            BGUTIL_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            BGUTIL_PROCESS.kill()
    BGUTIL_PROCESS = None


def human_youtube_error(error: Exception) -> str:
    text = str(error)
    if "Sign in to confirm you’re not a bot" in text:
        return "YouTube заблокував запит із сервера (anti-bot). Підключіть cookies."
    return text[:1000]


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if not TOKEN or not PUBLIC_URL:
    raise RuntimeError("BOT_TOKEN or PUBLIC_URL is missing!")

WEBHOOK_URL = f"{PUBLIC_URL}/telegram/webhook"


def is_youtube_url(url: str) -> bool:
    pattern = r"^https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/"
    return bool(re.match(pattern, url, re.IGNORECASE))


def is_youtube_music_url(url: str) -> bool:
    return bool(re.match(r"^https?://music\.youtube\.com/", url, re.IGNORECASE))


def get_video_formats_info(url: str):
    options = youtube_options_base()
    options.update({"quiet": True, "no_warnings": True, "skip_download": True})
    
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        
    formats = info.get("formats", [])
    
    audio_bytes = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            if sz > audio_bytes:
                audio_bytes = sz

    height_tiers = [1080, 720, 480, 360]
    available_qualities = []

    for h in height_tiers:
        video_bytes = 0
        found = False
        for f in formats:
            if f.get("vcodec") != "none" and f.get("height") == h:
                found = True
                sz = f.get("filesize") or f.get("filesize_approx") or 0
                if sz > video_bytes:
                    video_bytes = sz

        if found:
            total_bytes = video_bytes + audio_bytes
            mb = round(total_bytes / (1024 * 1024), 1) if total_bytes > 0 else 0
            available_qualities.append({"height": h, "size_mb": mb})

    return available_qualities, info.get("title", "video")


def download_audio(url: str, workdir: str):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    options = youtube_options_base()
    options.update({
        "format": "bestaudio/best",
        "outtmpl": output,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
    })
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp3_file = str(Path(filename).with_suffix(".mp3"))
        if os.path.exists(mp3_file):
            return mp3_file, info
        mp3_files = list(Path(workdir).glob("*.mp3"))
        if mp3_files:
            return str(mp3_files[0]), info
        raise FileNotFoundError("MP3 not found")


def download_video_quality(url: str, workdir: str, height: int):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    options = youtube_options_base()
    options.update({
        "format": f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/best[ext=mp4][height<={height}]/best[height<={height}]",
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    })
    
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4_file = str(Path(filename).with_suffix(".mp4"))
        
        if os.path.exists(mp4_file):
            return mp4_file, info
        
        video_files = [p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
        if video_files:
            return str(video_files[0]), info
            
        raise FileNotFoundError("Відеофайл не знайдено.")


# =========================================================
# TELEGRAM HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await check_user_language_or_ask(update, context)
    if lang is None:
        return
    await update.message.reply_text(get_text(lang, "start"))


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await check_user_language_or_ask(update, context)
    if lang is None:
        return
    keyboard = [
        [InlineKeyboardButton(get_text(lang, "change_lang"), callback_data="toggle_lang")],
        [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
    ]
    await update.message.reply_text(get_text(lang, "settings"), reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    lang = get_user_lang(user_id) or "ua"
    keyboard = [
        [InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin")],
        [InlineKeyboardButton(get_text(lang, "admin_add_channel_btn"), callback_data="admin_add_channel")],
        [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels")],
        [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
    ]
    await update.message.reply_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    lang = await check_user_language_or_ask(update, context)
    if lang is None:
        return

    url = update.message.text.strip()
    user_id = update.effective_user.id

    unsubscribed = await check_user_subscriptions(context.bot, user_id)
    if unsubscribed:
        keyboard = []
        for ch in unsubscribed:
            keyboard.append([InlineKeyboardButton(f"👉 {ch['title']}", url=ch['link'])])
        keyboard.append([InlineKeyboardButton(get_text(lang, "check_sub_btn"), callback_data="check_subscription")])
        
        await update.message.reply_text(
            get_text(lang, "sub_required"),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if not is_youtube_url(url):
        await update.message.reply_text(get_text(lang, "invalid_url"))
        return

    context.user_data["url"] = url
    await send_format_selection(update.message, url, lang)


async def send_format_selection(message, url: str, lang: str, is_edit=False):
    if is_youtube_music_url(url):
        keyboard = [[InlineKeyboardButton(get_text(lang, "download_audio"), callback_data="audio")]]
        text = "🎵 YouTube Music"
    else:
        keyboard = [[
            InlineKeyboardButton(get_text(lang, "audio_btn"), callback_data="audio"),
            InlineKeyboardButton(get_text(lang, "video_btn"), callback_data="video"),
        ]]
        text = get_text(lang, "choose_format")

    markup = InlineKeyboardMarkup(keyboard)
    if is_edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    # First-run language setting
    if data.startswith("set_lang:"):
        selected_lang = data.split(":")[1]
        set_user_lang(user_id, selected_lang)
        await query.edit_message_text(get_text(selected_lang, "lang_set"))
        await query.message.reply_text(get_text(selected_lang, "start"))
        return

    lang = get_user_lang(user_id) or "ua"

    if data == "close_menu":
        await query.message.delete()
        return

    if data == "toggle_lang":
        new_lang = "en" if lang == "ua" else "ua"
        set_user_lang(user_id, new_lang)
        keyboard = [[InlineKeyboardButton(get_text(new_lang, "close_btn"), callback_data="close_menu")]]
        await query.edit_message_text(get_text(new_lang, "lang_set"), reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "check_subscription":
        unsubscribed = await check_user_subscriptions(context.bot, user_id)
        if not unsubscribed:
            await query.edit_message_text(get_text(lang, "sub_success"))
        else:
            await query.answer(get_text(lang, "sub_failed"), show_alert=True)
        return

    # Admin Panel
    if data == "admin_menu":
        if not is_admin(user_id):
            return
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin")],
            [InlineKeyboardButton(get_text(lang, "admin_add_channel_btn"), callback_data="admin_add_channel")],
            [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await query.edit_message_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "admin_add_admin":
        if not is_admin(user_id):
            return
        context.user_data["admin_state"] = "await_admin_id"
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")]]
        await query.edit_message_text(get_text(lang, "admin_enter_admin_id"), reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_add_channel":
        if not is_admin(user_id):
            return
        context.user_data["admin_state"] = "await_channel_data"
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")]]
        await query.edit_message_text(
            get_text(lang, "admin_enter_channel_data"),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if data == "admin_list_channels":
        if not is_admin(user_id):
            return
        channels = get_sponsored_channels()
        keyboard = []
        if channels:
            for ch in channels:
                keyboard.append([InlineKeyboardButton(f"📢 {ch['title']}", callback_data=f"sp_view:{ch['id']}")])
        
        keyboard.append([InlineKeyboardButton(get_text(lang, "admin_add_channel_btn"), callback_data="admin_add_channel")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        
        text = get_text(lang, "sponsors_list_title") if channels else get_text(lang, "sponsors_empty")
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("sp_view:"):
        if not is_admin(user_id):
            return
        ch_id = data.split(":", 1)[1]
        ch = get_sponsored_channel(ch_id)
        if not ch:
            await query.answer("❌ Канал не знайдено!", show_alert=True)
            return

        text = get_text(lang, "sponsor_info").format(title=ch['title'], id=ch['id'], link=ch['link'])
        keyboard = [
            [
                InlineKeyboardButton(get_text(lang, "edit_btn"), callback_data=f"sp_edit:{ch_id}"),
                InlineKeyboardButton(get_text(lang, "delete_btn"), callback_data=f"sp_del:{ch_id}")
            ],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_list_channels")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("sp_del:"):
        if not is_admin(user_id):
            return
        ch_id = data.split(":", 1)[1]
        delete_sponsored_channel(ch_id)
        await query.answer(get_text(lang, "sponsor_deleted"), show_alert=True)
        
        channels = get_sponsored_channels()
        keyboard = []
        if channels:
            for ch in channels:
                keyboard.append([InlineKeyboardButton(f"📢 {ch['title']}", callback_data=f"sp_view:{ch['id']}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "admin_add_channel_btn"), callback_data="admin_add_channel")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        
        text = get_text(lang, "sponsors_list_title") if channels else get_text(lang, "sponsors_empty")
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("sp_edit:"):
        if not is_admin(user_id):
            return
        ch_id = data.split(":", 1)[1]
        context.user_data["admin_state"] = f"await_edit_channel:{ch_id}"
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"sp_view:{ch_id}")]]
        await query.edit_message_text(get_text(lang, "edit_sponsor_prompt"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "back_to_format":
        url = context.user_data.get("url")
        if url:
            await send_format_selection(query.message, url, lang, is_edit=True)
        else:
            await query.edit_message_text(get_text(lang, "link_lost"))
        return

    url = context.user_data.get("url")
    if not url:
        await query.edit_message_text(get_text(lang, "link_lost"))
        return

    if data == "video":
        status = await query.edit_message_text(get_text(lang, "fetching_qualities"))
        try:
            qualities, title = await asyncio.to_thread(get_video_formats_info, url)
            if not qualities:
                await status.edit_text(get_text(lang, "no_qualities"))
                return

            keyboard = []
            for q in qualities:
                size_str = f"~{q['size_mb']} МБ" if q['size_mb'] > 0 else "?"
                btn_text = f"🎬 {q['height']}p ({size_str})"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"vdl:{q['height']}")])

            keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="back_to_format")])

            await status.edit_text(
                get_text(lang, "choose_quality").format(title=title[:60]),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as error:
            logger.exception("Format fetch failed")
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        return

    if data.startswith("vdl:"):
        height = int(data.split(":")[1])
        status = await query.edit_message_text(get_text(lang, "downloading_video").format(height=height))
        workdir = tempfile.mkdtemp(prefix="yt_tg_")

        try:
            filepath, info = await asyncio.to_thread(download_video_quality, url, workdir, height)
            file_size = os.path.getsize(filepath)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=f,
                        supports_streaming=True,
                        duration=int(info.get("duration", 0)) or None,
                    )
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                web_path = DOWNLOADS_DIR / safe_name
                shutil.move(filepath, web_path)

                download_link = f"{PUBLIC_URL}/download/{safe_name}"
                mb_size = round(file_size / (1024 * 1024), 1)

                await status.edit_text(
                    get_text(lang, "file_too_large_video").format(mb=mb_size, link=download_link),
                    parse_mode="Markdown"
                )
        except Exception as error:
            logger.exception("Download failed")
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    if data == "audio":
        status = await query.edit_message_text(get_text(lang, "downloading_audio"))
        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            file_size = os.path.getsize(filepath)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=f,
                        title=info.get("title", "audio")[:64],
                        performer=info.get("artist") or info.get("uploader"),
                        duration=int(info.get("duration", 0)) or None,
                    )
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                web_path = DOWNLOADS_DIR / safe_name
                shutil.move(filepath, web_path)

                download_link = f"{PUBLIC_URL}/download/{safe_name}"
                mb_size = round(file_size / (1024 * 1024), 1)

                await status.edit_text(
                    get_text(lang, "file_too_large_audio").format(mb=mb_size, link=download_link),
                    parse_mode="Markdown"
                )
        except Exception as error:
            logger.exception("Download failed")
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    state = context.user_data.get("admin_state")
    if not state:
        return

    lang = get_user_lang(user_id) or "ua"
    text = update.message.text.strip()

    if state == "await_admin_id":
        if text.isdigit():
            add_admin(int(text))
            await update.message.reply_text(get_text(lang, "admin_admin_added").format(id=text), parse_mode="Markdown")
            context.user_data["admin_state"] = None
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_id"))

    elif state == "await_channel_data":
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            ch_id, title, link = parts
            add_sponsored_channel(ch_id, title, link)
            await update.message.reply_text(get_text(lang, "admin_channel_added").format(title=title), parse_mode="Markdown")
            context.user_data["admin_state"] = None
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_channel_format"))

    elif state.startswith("await_edit_channel:"):
        ch_id = state.split(":", 1)[1]
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            title, link = parts
            update_sponsored_channel(ch_id, title, link)
            await update.message.reply_text(get_text(lang, "sponsor_updated"), parse_mode="Markdown")
            context.user_data["admin_state"] = None
        else:
            await update.message.reply_text("❌ Формат: `Назва Посилання`")


# =========================================================
# APPLICATION SETUP & LIFESPAN
# =========================================================

telegram_app = Application.builder().token(TOKEN).updater(None).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("settings", settings_command))
telegram_app.add_handler(CommandHandler("admin", admin_command))

telegram_app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\d+$|^@"), handle_admin_text_input))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


async def keep_alive_ping():
    """Фонова задача для запобігання заснуванню веб-сервісу на Render."""
    await asyncio.sleep(10)
    while True:
        try:
            if PUBLIC_URL:
                ping_url = f"{PUBLIC_URL}/health"
                def do_ping():
                    import urllib.request
                    req = urllib.request.Request(ping_url, headers={"User-Agent": "RenderKeepAlive/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status
                status = await asyncio.to_thread(do_ping)
                logger.info("Keep-alive ping to %s returned status %s", ping_url, status)
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)
        await asyncio.sleep(600)  # Запит кожні 10 хвилин


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_bgutil_provider()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    ping_task = asyncio.create_task(keep_alive_ping())
    yield
    ping_task.cancel()
    try:
        await telegram_app.stop()
    finally:
        await telegram_app.shutdown()
        stop_bgutil_provider()


app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return PlainTextResponse("YouTube Bot is running.")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/download/{filename}")
async def get_download_file(filename: str):
    file_path = DOWNLOADS_DIR / filename
    if file_path.is_file():
        return FileResponse(file_path, media_type="application/octet-stream", filename=filename)
    raise HTTPException(status_code=404, detail="Файл не знайдено або термін дії посилання вичерпано")

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return PlainTextResponse("OK")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
