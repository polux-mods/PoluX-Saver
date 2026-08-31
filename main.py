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
from fastapi.responses import PlainTextResponse

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
MAX_PLAYLIST_ITEMS = 15
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()
DB_FILE = BASE_DIR / "bot_database.db"


# =========================================================
# DATABASE SYSTEM (SQLite)
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT DEFAULT 'ua'
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            invite_link TEXT
        )
    """)
    
    if INITIAL_ADMIN_ID > 0:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (INITIAL_ADMIN_ID,))
        
    conn.commit()
    conn.close()

def get_user_lang(user_id: int) -> str:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "ua"

def set_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, lang) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang
    """, (user_id, lang))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None or user_id == INITIAL_ADMIN_ID

def add_admin(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_sponsored_channels():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, title, invite_link FROM channels")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "link": r[2]} for r in rows]

def add_sponsored_channel(channel_id: str, title: str, link: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO channels (channel_id, title, invite_link) VALUES (?, ?, ?)",
                   (channel_id, title, link))
    conn.commit()
    conn.close()


# =========================================================
# LOCALIZATION (TEXTS)
# =========================================================

TEXTS = {
    "ua": {
        "start": "Привіт! 👋\nНадішли посилання на YouTube або YouTube Music.\nМожна відео, трек або плейлист.",
        "choose_format": "Обери формат:",
        "audio_btn": "🎵 Аудіо",
        "video_btn": "🎬 Відео",
        "download_audio": "🎵 Завантажити аудіо",
        "settings": "⚙️ Налаштування\nПоточна мова: Українська 🇺🇦",
        "change_lang": "Змінити мову",
        "lang_set": "Мову успішно змінено на Українську 🇺🇦",
        "sub_required": "⚠️ **Для використання бота підпишіться на наші канали-спонсори:**",
        "check_sub_btn": "🔄 Перевірити підписку",
        "sub_success": "✅ Дякуємо за підписку! Надішліть посилання ще раз.",
        "sub_failed": "❌ Ви підписалися не на всі канали!",
        "invalid_url": "❌ Надішли коректне посилання YouTube або YouTube Music.",
    },
    "en": {
        "start": "Hello! 👋\nSend a YouTube or YouTube Music link.\nVideo, track, or playlist supported.",
        "choose_format": "Choose format:",
        "audio_btn": "🎵 Audio",
        "video_btn": "🎬 Video",
        "download_audio": "🎵 Download audio",
        "settings": "⚙️ Settings\nCurrent language: English 🇬🇧",
        "change_lang": "Change language",
        "lang_set": "Language successfully set to English 🇬🇧",
        "sub_required": "⚠️ **Please subscribe to our sponsor channels to use the bot:**",
        "check_sub_btn": "🔄 Check subscription",
        "sub_success": "✅ Thank you for subscribing! Please send the link again.",
        "sub_failed": "❌ You have not subscribed to all channels!",
        "invalid_url": "❌ Please send a valid YouTube or YouTube Music link.",
    }
}


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


def extract_info(url: str):
    options = youtube_options_base()
    options.update({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"})
    with YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


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


def download_video(url: str, workdir: str):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    options = youtube_options_base()
    options.update({
        "format": "best[ext=mp4][height<=720]/bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best",
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
        raise FileNotFoundError("Video not found")


# =========================================================
# TELEGRAM HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(TEXTS[lang]["start"])


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_user_lang(update.effective_user.id)
    keyboard = [[InlineKeyboardButton(TEXTS[lang]["change_lang"], callback_data="toggle_lang")]]
    await update.message.reply_text(TEXTS[lang]["settings"], reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    keyboard = [
        [InlineKeyboardButton("➕ Додати адміна", callback_data="admin_add_admin")],
        [InlineKeyboardButton("📢 Додати спонсорський канал", callback_data="admin_add_channel")],
        [InlineKeyboardButton("📋 Список спонсорів", callback_data="admin_list_channels")],
    ]
    await update.message.reply_text("🔑 **Панель адміністратора**", reply_markup=InlineKeyboardMarkup(keyboard))


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)

    unsubscribed = await check_user_subscriptions(context.bot, user_id)
    if unsubscribed:
        keyboard = []
        for ch in unsubscribed:
            keyboard.append([InlineKeyboardButton(f"👉 {ch['title']}", url=ch['link'])])
        keyboard.append([InlineKeyboardButton(TEXTS[lang]["check_sub_btn"], callback_data="check_subscription")])
        
        await update.message.reply_text(
            TEXTS[lang]["sub_required"],
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if not is_youtube_url(url):
        await update.message.reply_text(TEXTS[lang]["invalid_url"])
        return

    context.user_data["url"] = url

    if is_youtube_music_url(url):
        keyboard = [[InlineKeyboardButton(TEXTS[lang]["download_audio"], callback_data="audio")]]
        text = "🎵 YouTube Music"
    else:
        keyboard = [[
            InlineKeyboardButton(TEXTS[lang]["audio_btn"], callback_data="audio"),
            InlineKeyboardButton(TEXTS[lang]["video_btn"], callback_data="video"),
        ]]
        text = TEXTS[lang]["choose_format"]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    data = query.data

    if data == "toggle_lang":
        new_lang = "en" if lang == "ua" else "ua"
        set_user_lang(user_id, new_lang)
        await query.edit_message_text(TEXTS[new_lang]["lang_set"])
        return

    if data == "check_subscription":
        unsubscribed = await check_user_subscriptions(context.bot, user_id)
        if not unsubscribed:
            await query.edit_message_text(TEXTS[lang]["sub_success"])
        else:
            await query.answer(TEXTS[lang]["sub_failed"], show_alert=True)
        return

    if data == "admin_add_admin":
        context.user_data["admin_state"] = "await_admin_id"
        await query.edit_message_text("Надішліть Telegram ID користувача, якому хочете надати права адміна:")
        return

    if data == "admin_add_channel":
        context.user_data["admin_state"] = "await_channel_data"
        await query.edit_message_text(
            "Надішліть дані каналу у такому форматі (через пробіл):\n"
            "`@channel_id Назва_Каналу https://t.me/link`\n\n"
            "⚠️ **Бот повинен бути доданий в цей канал як АДМІНІСТРАТОР!**",
            parse_mode="Markdown"
        )
        return

    if data == "admin_list_channels":
        channels = get_sponsored_channels()
        if not channels:
            await query.edit_message_text("Спонсорських каналів немає.")
            return
        msg = "📋 **Спонсорські канали:**\n\n"
        for ch in channels:
            msg += f"• {ch['title']} ({ch['id']})\n{ch['link']}\n\n"
        await query.edit_message_text(msg, parse_mode="Markdown")
        return

    url = context.user_data.get("url")
    if not url:
        await query.edit_message_text("❌ Посилання втрачено. Надішли його ще раз.")
        return

    status = await query.edit_message_text("⏳ Завантажую...")
    workdir = tempfile.mkdtemp(prefix="yt_tg_")

    try:
        if data == "audio":
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            with open(filepath, "rb") as f:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    title=info.get("title", "audio")[:64],
                    performer=info.get("artist") or info.get("uploader"),
                    duration=int(info.get("duration", 0)) or None,
                )
        else:
            filepath, info = await asyncio.to_thread(download_video, url, workdir)
            with open(filepath, "rb") as f:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    supports_streaming=True,
                    duration=int(info.get("duration", 0)) or None,
                )
        await status.delete()
    except Exception as error:
        logger.exception("Download failed")
        await status.edit_text(f"❌ Помилка: {human_youtube_error(error)}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    state = context.user_data.get("admin_state")
    if not state:
        return

    text = update.message.text.strip()

    if state == "await_admin_id":
        if text.isdigit():
            add_admin(int(text))
            await update.message.reply_text(f"✅ Користувача `{text}` успішно додано до адмінів!", parse_mode="Markdown")
            context.user_data["admin_state"] = None
        else:
            await update.message.reply_text("❌ Введіть числовий Telegram ID.")

    elif state == "await_channel_data":
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            ch_id, title, link = parts
            add_sponsored_channel(ch_id, title, link)
            await update.message.reply_text(f"✅ Канал `{title}` додано до списку спонсорів!", parse_mode="Markdown")
            context.user_data["admin_state"] = None
        else:
            await update.message.reply_text("❌ Невірний формат. Введіть: `@channel_id Назва Посилання`")


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
    yield
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
