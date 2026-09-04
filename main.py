import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeAllPrivateChats
)
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
COOKIES_FILE_PATH = BASE_DIR / "cookies.txt"
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()

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
        conn = sqlite3.connect(BASE_DIR / "bot_database.db")
        return conn, False

def execute_query(query: str, params: tuple = (), fetchone=False, fetchall=False, commit=False):
    conn, is_postgres = get_db_connection()
    try:
        cursor = conn.cursor()
        sql = query
        if is_postgres:
            sql = sql.replace("?", "%s").replace("excluded.", "EXCLUDED.")
            # Adjust auto-increment for Postgres
            if "AUTOINCREMENT" in sql:
                sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
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

def sync_cookies_from_db():
    row = execute_query("SELECT value FROM settings WHERE key = ?", ("youtube_cookies",), fetchone=True)
    if row and row[0]:
        COOKIES_FILE_PATH.write_text(row[0], encoding="utf-8")
    elif YOUTUBE_COOKIES:
        COOKIES_FILE_PATH.write_text(YOUTUBE_COOKIES, encoding="utf-8")

def save_db_cookies(content: str):
    execute_query("""
        INSERT INTO settings (key, value) VALUES ('youtube_cookies', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
    """, (content,), commit=True)
    COOKIES_FILE_PATH.write_text(content, encoding="utf-8")

def init_db():
    # USERS
    execute_query("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            lang TEXT
        )
    """, commit=True)
    
    # Migrations for Users
    try: execute_query("ALTER TABLE users ADD COLUMN username TEXT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN first_name TEXT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN downloads INTEGER DEFAULT 0", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN joined_date TEXT", commit=True)
    except: pass

    # ADMINS
    execute_query("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        )
    """, commit=True)
    
    # Migrations for Admins
    try: execute_query("ALTER TABLE admins ADD COLUMN added_by BIGINT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE admins ADD COLUMN added_date TEXT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE admins ADD COLUMN username TEXT", commit=True)
    except: pass

    # CHANNELS
    execute_query("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            invite_link TEXT
        )
    """, commit=True)

    # SETTINGS
    execute_query("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """, commit=True)
    
    # HISTORY
    execute_query("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT,
            url TEXT,
            download_date TEXT
        )
    """, commit=True)

    if INITIAL_ADMIN_ID > 0:
        date_now = datetime.now().strftime("%Y-%m-%d %H:%M")
        execute_query("""
            INSERT INTO admins (user_id, added_date, username) VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO NOTHING
        """, (INITIAL_ADMIN_ID, date_now, "Owner"), commit=True)

    sync_cookies_from_db()


# --- User & Admin DB Helpers ---

def register_or_update_user(user_id: int, username: str, first_name: str, lang: str = "ua"):
    date_now = datetime.now().strftime("%Y-%m-%d")
    row = execute_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row:
        execute_query("""
            INSERT INTO users (user_id, lang, username, first_name, joined_date, downloads, is_banned)
            VALUES (?, ?, ?, ?, ?, 0, FALSE)
        """, (user_id, lang, username, first_name, date_now), commit=True)
    else:
        execute_query("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?", 
                      (username, first_name, user_id), commit=True)

def get_user_info(user_id: int):
    return execute_query("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)

def is_user_banned(user_id: int) -> bool:
    row = execute_query("SELECT is_banned FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row else False

def set_user_ban(user_id: int, state: bool):
    execute_query("UPDATE users SET is_banned = ? WHERE user_id = ?", (state, user_id), commit=True)

def increment_downloads(user_id: int, url: str):
    execute_query("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,), commit=True)
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M")
    execute_query("INSERT INTO history (user_id, url, download_date) VALUES (?, ?, ?)", (user_id, url, date_now), commit=True)

def get_user_history(user_id: int, limit=5):
    return execute_query("SELECT url, download_date FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit), fetchall=True)

def get_user_lang(user_id: int) -> str:
    row = execute_query("SELECT lang FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row and row[0] else "ua"

def set_user_lang(user_id: int, lang: str):
    execute_query("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id), commit=True)

def is_admin(user_id: int) -> bool:
    if user_id == INITIAL_ADMIN_ID:
        return True
    row = execute_query("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    return row is not None

def add_admin(user_id: int, added_by: int, username: str = None):
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M")
    execute_query("""
        INSERT INTO admins (user_id, added_by, added_date, username) VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id, added_by, date_now, username), commit=True)

def remove_admin(user_id: int):
    if user_id != INITIAL_ADMIN_ID:
        execute_query("DELETE FROM admins WHERE user_id = ?", (user_id,), commit=True)

def get_all_admins_info():
    return execute_query("SELECT user_id, added_by, added_date, username FROM admins", fetchall=True)

def get_admin_info(user_id: int):
    return execute_query("SELECT user_id, added_by, added_date, username FROM admins WHERE user_id = ?", (user_id,), fetchone=True)

def get_sponsored_channels():
    rows = execute_query("SELECT channel_id, title, invite_link FROM channels", fetchall=True)
    return [{"id": r[0], "title": r[1], "link": r[2]} for r in rows] if rows else []

def get_sponsored_channel(channel_id: str):
    row = execute_query("SELECT channel_id, title, invite_link FROM channels WHERE channel_id = ?", (channel_id,), fetchone=True)
    return {"id": row[0], "title": row[1], "link": row[2]} if row else None

def add_sponsored_channel(channel_id: str, title: str, link: str):
    execute_query("""
        INSERT INTO channels (channel_id, title, invite_link) VALUES (?, ?, ?)
        ON CONFLICT (channel_id) DO UPDATE SET title = excluded.title, invite_link = excluded.invite_link
    """, (channel_id, title, link), commit=True)

def update_sponsored_channel(channel_id: str, title: str, link: str):
    execute_query("UPDATE channels SET title = ?, invite_link = ? WHERE channel_id = ?", (title, link, channel_id), commit=True)

def delete_sponsored_channel(channel_id: str):
    execute_query("DELETE FROM channels WHERE channel_id = ?", (channel_id,), commit=True)


# =========================================================
# LOCALIZATION (TEXTS)
# =========================================================

TEXTS = {
    "ua": {
        "btn_settings": "⚙️ Налаштування",
        "btn_profile": "👤 Профіль",
        "btn_admin": "🔑 Адмін меню",
        "start": "Привіт! 👋\nНадішли посилання на YouTube або YouTube Music.\nМожна відео, трек або плейлист.",
        "choose_format": "Обери формат:",
        "audio_btn": "🎵 Аудіо",
        "video_btn": "🎬 Відео",
        "download_audio": "🎵 Завантажити аудіо",
        
        "settings_main": "⚙️ **Налаштування**\nОберіть потрібний розділ:",
        "lang_menu_btn": "🌐 Мова",
        "settings_lang": "Оберіть бажану мову інтерфейсу:",
        "lang_set": "✅ Мову успішно змінено на Українську 🇺🇦",
        
        "profile_text": "👤 **Профіль користувача**\n\n**Ім'я:** {name}\n**ID:** `{id}`\n**Статус:** {status}\n**Дата реєстрації:** {date}\n**Завантажень:** {downloads}",
        "status_user": "Користувач 👤",
        "status_admin": "Адміністратор 👑",
        
        "sub_required": "⚠️ **Для використання бота підпишіться на наші канали-спонсори:**",
        "check_sub_btn": "🔄 Перевірити підписку",
        "sub_success": "✅ Дякуємо за підписку! Надішліть посилання ще раз.",
        "sub_failed": "❌ Ви підписалися не на всі канали!",
        "invalid_url": "❌ Надішли коректне посилання YouTube або YouTube Music.",
        "banned_text": "❌ Ваш акаунт заблоковано. Ви не можете користуватись ботом.",
        
        "back_btn": "🔙 Назад",
        "cancel_btn": "❌ Скасувати",
        "close_btn": "❌ Закрити",
        
        "fetching_qualities": "🔎 Отримую список доступних якостей...",
        "no_qualities": "❌ Не вдалося отримати варіанти якості для цього відео.",
        "choose_quality": "📹 **{title}**\n\nОберіть бажану якість:",
        "downloading_video": "⏳ Завантажую відео у якості {height}p...",
        "downloading_audio": "⏳ Завантажую аудіо та обкладинку...",
        "file_too_large_video": "📦 **Файл перевищує 50 МБ** ({mb} МБ).\n\n🔗 [Натисніть сюди, щоб завантажити відео]({link})",
        "file_too_large_audio": "📦 **Аудіо перевищує 50 МБ** ({mb} МБ).\n\n🔗 [Натисніть сюди, щоб завантажити аудіо]({link})",
        "link_lost": "❌ Посилання втрачено. Надішли його ще раз.",
        
        # Адмін панель
        "admin_title": "🔑 **Панель адміністратора**",
        "admin_list_admins_btn": "👥 Список адмінів",
        "admin_add_admin_btn": "➕ Додати адміна",
        "admin_add_channel_btn": "📢 Додати спонсора",
        "admin_list_channels_btn": "📋 Список спонсорів",
        "admin_users_btn": "🔍 Користувачі",
        "admin_cookies_btn": "🍪 Оновити Cookies",
        
        "admin_enter_admin_id": "Надішліть Telegram ID користувача, якому хочете надати права адміна:",
        "admin_enter_channel_data": "Надішліть дані каналу:\n`@channel_id Назва_Каналу https://t.me/link`",
        "admin_enter_cookies": "🍪 Надішліть файл `cookies.txt` або його текст.\n*(Зараз бот не реагує на інші посилання)*",
        "cookies_updated": "✅ Cookies успішно збережено в хмару та оновлено!",
        "admin_channel_added": "✅ Канал `{title}` додано до спонсорів!",
        "admin_invalid_channel_format": "❌ Невірний формат. Введіть: `@channel_id Назва Посилання`",
        "admin_admin_added": "✅ Користувача успішно додано до адмінів!",
        "admin_invalid_id": "❌ Введіть числовий Telegram ID.",
        
        "sponsors_empty": "📋 **Спонсорських каналів немає.**",
        "sponsors_list_title": "📋 **Керування спонсорами:**",
        "sponsor_info": "📢 **Канал:** {title}\n🆔 **ID:** `{id}`\n🔗 **Посилання:** {link}",
        "edit_btn": "✏️ Редагувати",
        "delete_btn": "🗑 Видалити",
        "sponsor_deleted": "✅ Канал видалено!",
        "edit_sponsor_prompt": "Надішліть нову назву та посилання через пробіл:\n`Нова_Назва https://t.me/link`",
        "sponsor_updated": "✅ Дані каналу оновлено!",
        
        "admins_list_title": "👥 **Список адміністраторів:**",
        "admin_info": "👑 **Адміністратор:** {name}\n🆔 **ID:** `{id}`\n📅 **Доданий:** {date}\n👤 **Ким доданий:** `{added_by}`",
        "admin_deleted": "✅ Адміністратора видалено!",
        "cant_delete_owner": "❌ Головного адміна видалити неможливо!",
        
        "admin_search_user_prompt": "🔍 Надішліть Telegram ID користувача для пошуку:",
        "user_not_found": "❌ Користувача з таким ID не знайдено в базі.",
        "user_info_admin": "👤 **Профіль:** {name}\n🆔 **ID:** `{id}`\n📅 **Реєстрація:** {date}\n📥 **Завантажень:** {downloads}\n🚫 **Бан:** {banned}",
        "ban_btn": "🚫 Забанити",
        "unban_btn": "✅ Розбанити",
        "history_btn": "🕒 Історія",
        "user_banned_success": "✅ Користувача забанено.",
        "user_unbanned_success": "✅ Користувача розбанено.",
        "user_history_title": "🕒 **Останні завантаження ({id}):**\n",
        "user_history_empty": "Історія порожня.",
        
        "action_cancelled": "✅ Дія скасована."
    },
    "en": {
        "btn_settings": "⚙️ Settings",
        "btn_profile": "👤 Profile",
        "btn_admin": "🔑 Admin Panel",
        "start": "Hello! 👋\nSend a YouTube or YouTube Music link.\nVideo, track, or playlist supported.",
        "choose_format": "Choose format:",
        "audio_btn": "🎵 Audio",
        "video_btn": "🎬 Video",
        "download_audio": "🎵 Download audio",
        
        "settings_main": "⚙️ **Settings**\nSelect a section:",
        "lang_menu_btn": "🌐 Language",
        "settings_lang": "Select your preferred interface language:",
        "lang_set": "✅ Language successfully set to English 🇬🇧",
        
        "profile_text": "👤 **User Profile**\n\n**Name:** {name}\n**ID:** `{id}`\n**Status:** {status}\n**Joined:** {date}\n**Downloads:** {downloads}",
        "status_user": "User 👤",
        "status_admin": "Administrator 👑",
        
        "sub_required": "⚠️ **Please subscribe to our sponsor channels:**",
        "check_sub_btn": "🔄 Check subscription",
        "sub_success": "✅ Thank you for subscribing! Please send the link again.",
        "sub_failed": "❌ You have not subscribed to all channels!",
        "invalid_url": "❌ Please send a valid YouTube or YouTube Music link.",
        "banned_text": "❌ Your account is banned. You cannot use this bot.",
        
        "back_btn": "🔙 Back",
        "cancel_btn": "❌ Cancel",
        "close_btn": "❌ Close",
        
        "fetching_qualities": "🔎 Fetching available video qualities...",
        "no_qualities": "❌ Could not get quality options for this video.",
        "choose_quality": "📹 **{title}**\n\nChoose desired quality:",
        "downloading_video": "⏳ Downloading video in {height}p quality...",
        "downloading_audio": "⏳ Downloading audio & cover art...",
        "file_too_large_video": "📦 **File exceeds 50 MB** ({mb} MB).\n\n🔗 [Click here to download video]({link})",
        "file_too_large_audio": "📦 **Audio exceeds 50 MB** ({mb} MB).\n\n🔗 [Click here to download audio]({link})",
        "link_lost": "❌ Link lost. Please send it again.",
        
        "admin_title": "🔑 **Admin Panel**",
        "admin_list_admins_btn": "👥 Admins List",
        "admin_add_admin_btn": "➕ Add Admin",
        "admin_add_channel_btn": "📢 Add Sponsor",
        "admin_list_channels_btn": "📋 Sponsor List",
        "admin_users_btn": "🔍 Users",
        "admin_cookies_btn": "🍪 Update Cookies",
        
        "admin_enter_admin_id": "Send the Telegram ID to promote to admin:",
        "admin_enter_channel_data": "Send channel details:\n`@channel_id Name https://t.me/link`",
        "admin_enter_cookies": "🍪 Send the `cookies.txt` file or paste text.\n*(Bot ignores YouTube links in this state)*",
        "cookies_updated": "✅ Cookies successfully saved!",
        "admin_channel_added": "✅ Channel added to sponsors!",
        "admin_invalid_channel_format": "❌ Invalid format.",
        "admin_admin_added": "✅ User added as admin!",
        "admin_invalid_id": "❌ Please enter a numeric Telegram ID.",
        
        "sponsors_empty": "📋 **No sponsor channels found.**",
        "sponsors_list_title": "📋 **Sponsors Management:**",
        "sponsor_info": "📢 **Channel:** {title}\n🆔 **ID:** `{id}`\n🔗 **Link:** {link}",
        "edit_btn": "✏️ Edit",
        "delete_btn": "🗑 Delete",
        "sponsor_deleted": "✅ Channel deleted!",
        "edit_sponsor_prompt": "Send new name and link:\n`New_Name https://t.me/link`",
        "sponsor_updated": "✅ Channel updated!",
        
        "admins_list_title": "👥 **Administrators List:**",
        "admin_info": "👑 **Admin:** {name}\n🆔 **ID:** `{id}`\n📅 **Added:** {date}\n👤 **Added by:** `{added_by}`",
        "admin_deleted": "✅ Admin removed!",
        "cant_delete_owner": "❌ Cannot remove the main owner!",
        
        "admin_search_user_prompt": "🔍 Send Telegram ID to search:",
        "user_not_found": "❌ User not found in DB.",
        "user_info_admin": "👤 **Profile:** {name}\n🆔 **ID:** `{id}`\n📅 **Joined:** {date}\n📥 **Downloads:** {downloads}\n🚫 **Banned:** {banned}",
        "ban_btn": "🚫 Ban",
        "unban_btn": "✅ Unban",
        "history_btn": "🕒 History",
        "user_banned_success": "✅ User banned.",
        "user_unbanned_success": "✅ User unbanned.",
        "user_history_title": "🕒 **Recent Downloads ({id}):**\n",
        "user_history_empty": "History is empty.",
        
        "action_cancelled": "✅ Action cancelled."
    }
}

def get_text(lang: str, key: str) -> str:
    l = lang if lang in TEXTS else "ua"
    return TEXTS[l].get(key, TEXTS["ua"].get(key, key))


# =========================================================
# KEYBOARDS & COMMANDS
# =========================================================

async def setup_bot_commands(app_bot):
    # Only leaving technical restart, standard UI is buttons
    await app_bot.set_my_commands([BotCommand("start", "Restart / Запустити")], scope=BotCommandScopeAllPrivateChats())

def get_main_keyboard(user_id: int, lang: str) -> ReplyKeyboardMarkup:
    keys = [
        [KeyboardButton(get_text(lang, "btn_settings")), KeyboardButton(get_text(lang, "btn_profile"))]
    ]
    if is_admin(user_id):
        keys.append([KeyboardButton(get_text(lang, "btn_admin"))])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)

def get_cancel_inline(lang: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data=callback_data)]])


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
# YT-DLP CORE LOGIC (UNCHANGED)
# =========================================================

def youtube_options_base():
    options = {
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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

    if COOKIES_FILE_PATH.is_file():
        options["cookiefile"] = str(COOKIES_FILE_PATH)

    return options


def start_bgutil_provider():
    global BGUTIL_PROCESS
    if not BGUTIL_MAIN.is_file() or not (LOCAL_NODE_BIN / "node").is_file():
        return False

    BGUTIL_PROCESS = subprocess.Popen(
        [str(LOCAL_NODE_BIN / "node"), str(BGUTIL_MAIN), "--port", str(BGUTIL_PORT)],
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
        return "YouTube заблокував запит (anti-bot). Оновіть cookies в адмін-панелі."
    return text[:1000]

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
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
    })

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            mp3_file = str(Path(filename).with_suffix(".mp3"))
            if os.path.exists(mp3_file): return mp3_file, info
            mp3_files = list(Path(workdir).glob("*.mp3"))
            if mp3_files: return str(mp3_files[0]), info
    except Exception as e:
        logger.warning("Cover embed failed (%s). Fallback to clean MP3...", e)
        fallback_options = youtube_options_base()
        fallback_options.update({
            "format": "bestaudio/best",
            "outtmpl": output,
            "writethumbnail": False,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        })
        with YoutubeDL(fallback_options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            mp3_file = str(Path(filename).with_suffix(".mp3"))
            if os.path.exists(mp3_file): return mp3_file, info
            mp3_files = list(Path(workdir).glob("*.mp3"))
            if mp3_files: return str(mp3_files[0]), info

    raise FileNotFoundError("MP3 file not found.")

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
        
        if os.path.exists(mp4_file): return mp4_file, info
        video_files = [p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
        if video_files: return str(video_files[0]), info
        raise FileNotFoundError("Video file not found.")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

if not TOKEN or not PUBLIC_URL:
    raise RuntimeError("BOT_TOKEN or PUBLIC_URL is missing!")
WEBHOOK_URL = f"{PUBLIC_URL}/telegram/webhook"


# =========================================================
# TELEGRAM HANDLERS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    first_name = user.first_name or ""
    register_or_update_user(user.id, username, first_name)
    
    lang = get_user_lang(user.id)
    await update.message.reply_text(
        get_text(lang, "start"),
        reply_markup=get_main_keyboard(user.id, lang)
    )

async def master_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip() if update.message.text else ""
    
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    
    if is_user_banned(user.id):
        lang = get_user_lang(user.id)
        await update.message.reply_text(get_text(lang, "banned_text"))
        return

    lang = get_user_lang(user.id)
    
    # 1. Захист від посилань під час очікування введення від адміна (напр. Cookies)
    admin_state = context.user_data.get("admin_state")
    if admin_state:
        await handle_admin_inputs(update, context, text, admin_state, lang)
        return

    # 2. Кнопки головного меню
    if text in [TEXTS["ua"]["btn_settings"], TEXTS["en"]["btn_settings"]]:
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "lang_menu_btn"), callback_data="settings_lang")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await update.message.reply_text(get_text(lang, "settings_main"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if text in [TEXTS["ua"]["btn_profile"], TEXTS["en"]["btn_profile"]]:
        info = get_user_info(user.id)
        status = get_text(lang, "status_admin") if is_admin(user.id) else get_text(lang, "status_user")
        profile_msg = get_text(lang, "profile_text").format(
            name=info[3] or info[2] or "User",
            id=user.id,
            status=status,
            date=info[6] if len(info)>6 else "-",
            downloads=info[4] if len(info)>4 else 0
        )
        await update.message.reply_text(profile_msg, parse_mode="Markdown")
        return

    if text in [TEXTS["ua"]["btn_admin"], TEXTS["en"]["btn_admin"]]:
        if not is_admin(user.id): return
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users")],
            [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels")],
            [InlineKeyboardButton(get_text(lang, "admin_cookies_btn"), callback_data="admin_cookies")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await update.message.reply_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # 3. Обробка посилань Youtube
    if text:
        unsubscribed = await check_user_subscriptions(context.bot, user.id)
        if unsubscribed:
            keyboard = [[InlineKeyboardButton(f"👉 {ch['title']}", url=ch['link'])] for ch in unsubscribed]
            keyboard.append([InlineKeyboardButton(get_text(lang, "check_sub_btn"), callback_data="check_subscription")])
            await update.message.reply_text(get_text(lang, "sub_required"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        if not is_youtube_url(text):
            await update.message.reply_text(get_text(lang, "invalid_url"))
            return

        context.user_data["url"] = text
        await send_format_selection(update.message, text, lang)


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
    lang = get_user_lang(user_id)

    if is_user_banned(user_id):
        await query.edit_message_text(get_text(lang, "banned_text"))
        return

    if data == "close_menu":
        context.user_data["admin_state"] = None
        await query.message.delete()
        return
        
    if data == "cancel_admin_action":
        context.user_data["admin_state"] = None
        await query.edit_message_text(get_text(lang, "action_cancelled"))
        return
        
    if data == "cancel_to_admin_menu":
        context.user_data["admin_state"] = None
        data = "admin_menu"

    if data == "settings_lang":
        keyboard = [
            [InlineKeyboardButton("🇺🇦 Українська", callback_data="set_lang:ua"), InlineKeyboardButton("🇬🇧 English", callback_data="set_lang:en")],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="settings_main")]
        ]
        await query.edit_message_text(get_text(lang, "settings_lang"), reply_markup=InlineKeyboardMarkup(keyboard))
        return
        
    if data == "settings_main":
        keyboard = [[InlineKeyboardButton(get_text(lang, "lang_menu_btn"), callback_data="settings_lang")], [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]]
        await query.edit_message_text(get_text(lang, "settings_main"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("set_lang:"):
        new_lang = data.split(":")[1]
        set_user_lang(user_id, new_lang)
        await query.message.delete()
        await query.message.reply_text(get_text(new_lang, "lang_set"), reply_markup=get_main_keyboard(user_id, new_lang))
        return

    if data == "check_subscription":
        unsubscribed = await check_user_subscriptions(context.bot, user_id)
        if not unsubscribed:
            await query.edit_message_text(get_text(lang, "sub_success"))
        else:
            await query.answer(get_text(lang, "sub_failed"), show_alert=True)
        return

    # --- ADMIN CALLBACKS ---
    if data == "admin_menu" and is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users")],
            [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels")],
            [InlineKeyboardButton(get_text(lang, "admin_cookies_btn"), callback_data="admin_cookies")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await query.edit_message_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "admin_list_admins" and is_admin(user_id):
        admins = get_all_admins_info()
        keyboard = []
        for adm in admins:
            name = adm[3] or f"ID: {adm[0]}"
            keyboard.append([InlineKeyboardButton(f"👑 {name}", callback_data=f"adm_view:{adm[0]}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "admins_list_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("adm_view:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        info = get_admin_info(adm_id)
        if not info: return
        text = get_text(lang, "admin_info").format(name=info[3] or "Unknown", id=info[0], date=info[2] or "-", added_by=info[1] or "-")
        keyboard = []
        if adm_id != INITIAL_ADMIN_ID:
            keyboard.append([InlineKeyboardButton(get_text(lang, "delete_btn"), callback_data=f"adm_del:{adm_id}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_list_admins")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("adm_del:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        if adm_id == INITIAL_ADMIN_ID:
            await query.answer(get_text(lang, "cant_delete_owner"), show_alert=True)
            return
        remove_admin(adm_id)
        await query.answer(get_text(lang, "admin_deleted"), show_alert=True)
        # return to list
        admins = get_all_admins_info()
        keyboard = [[InlineKeyboardButton(f"👑 {adm[3] or adm[0]}", callback_data=f"adm_view:{adm[0]}")] for adm in admins]
        keyboard.append([InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin"), InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "admins_list_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "admin_add_admin" and is_admin(user_id):
        context.user_data["admin_state"] = "await_admin_id"
        await query.edit_message_text(get_text(lang, "admin_enter_admin_id"), reply_markup=get_cancel_inline(lang, "cancel_to_admin_menu"))
        return

    if data == "admin_cookies" and is_admin(user_id):
        context.user_data["admin_state"] = "await_cookies"
        await query.edit_message_text(get_text(lang, "admin_enter_cookies"), reply_markup=get_cancel_inline(lang, "cancel_to_admin_menu"), parse_mode="Markdown")
        return
        
    if data == "admin_users" and is_admin(user_id):
        context.user_data["admin_state"] = "await_user_search"
        await query.edit_message_text(get_text(lang, "admin_search_user_prompt"), reply_markup=get_cancel_inline(lang, "cancel_to_admin_menu"))
        return
        
    if data.startswith("usr_ban:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        set_user_ban(target_id, True)
        await query.answer(get_text(lang, "user_banned_success"), show_alert=True)
        # rebuild user view
        data = f"usr_view:{target_id}"
        
    if data.startswith("usr_unban:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        set_user_ban(target_id, False)
        await query.answer(get_text(lang, "user_unbanned_success"), show_alert=True)
        data = f"usr_view:{target_id}"

    if data.startswith("usr_view:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        info = get_user_info(target_id)
        if not info:
            await query.answer(get_text(lang, "user_not_found"), show_alert=True)
            return
        
        banned = info[5]
        text = get_text(lang, "user_info_admin").format(
            name=info[3] or info[2] or "User", id=info[0], date=info[6] if len(info)>6 else "-", downloads=info[4] if len(info)>4 else 0,
            banned="🔴 Так" if banned else "🟢 Ні"
        )
        keyboard = [[InlineKeyboardButton(get_text(lang, "history_btn"), callback_data=f"usr_hist:{target_id}")]]
        if banned: keyboard[0].append(InlineKeyboardButton(get_text(lang, "unban_btn"), callback_data=f"usr_unban:{target_id}"))
        else: keyboard[0].append(InlineKeyboardButton(get_text(lang, "ban_btn"), callback_data=f"usr_ban:{target_id}"))
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_users")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("usr_hist:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        hist = get_user_history(target_id, 10)
        text = get_text(lang, "user_history_title").format(id=target_id)
        if not hist:
            text += get_text(lang, "user_history_empty")
        else:
            for i, h in enumerate(hist):
                text += f"{i+1}. `{h[1]}`\n🔗 {h[0]}\n"
        
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"usr_view:{target_id}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown", disable_web_page_preview=True)
        return

    if data == "admin_add_channel" and is_admin(user_id):
        context.user_data["admin_state"] = "await_channel_data"
        await query.edit_message_text(get_text(lang, "admin_enter_channel_data"), reply_markup=get_cancel_inline(lang, "admin_list_channels"), parse_mode="Markdown")
        return

    if data == "admin_list_channels" and is_admin(user_id):
        channels = get_sponsored_channels()
        keyboard = [[InlineKeyboardButton(f"📢 {ch['title']}", callback_data=f"sp_view:{ch['id']}")] for ch in channels]
        keyboard.append([InlineKeyboardButton(get_text(lang, "admin_add_channel_btn"), callback_data="admin_add_channel")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        text = get_text(lang, "sponsors_list_title") if channels else get_text(lang, "sponsors_empty")
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("sp_view:") and is_admin(user_id):
        ch_id = data.split(":", 1)[1]
        ch = get_sponsored_channel(ch_id)
        if not ch: return
        text = get_text(lang, "sponsor_info").format(title=ch['title'], id=ch['id'], link=ch['link'])
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "edit_btn"), callback_data=f"sp_edit:{ch_id}"), InlineKeyboardButton(get_text(lang, "delete_btn"), callback_data=f"sp_del:{ch_id}")],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_list_channels")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("sp_del:") and is_admin(user_id):
        ch_id = data.split(":", 1)[1]
        delete_sponsored_channel(ch_id)
        await query.answer(get_text(lang, "sponsor_deleted"), show_alert=True)
        # redirect to list
        channels = get_sponsored_channels()
        keyboard = [[InlineKeyboardButton(f"📢 {ch['title']}", callback_data=f"sp_view:{ch['id']}")] for ch in channels]
        keyboard.append([InlineKeyboardButton(get_text(lang, "admin_add_channel_btn"), callback_data="admin_add_channel"), InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "sponsors_list_title") if channels else get_text(lang, "sponsors_empty"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("sp_edit:") and is_admin(user_id):
        ch_id = data.split(":", 1)[1]
        context.user_data["admin_state"] = f"await_edit_channel:{ch_id}"
        await query.edit_message_text(get_text(lang, "edit_sponsor_prompt"), reply_markup=get_cancel_inline(lang, f"sp_view:{ch_id}"), parse_mode="Markdown")
        return

    # --- DOWNLOAD LOGIC CALLBACKS ---
    if data == "back_to_format":
        url = context.user_data.get("url")
        if url: await send_format_selection(query.message, url, lang, is_edit=True)
        else: await query.edit_message_text(get_text(lang, "link_lost"))
        return

    url = context.user_data.get("url")
    if not url:
        return

    if data == "video":
        status = await query.edit_message_text(get_text(lang, "fetching_qualities"))
        try:
            qualities, title = await asyncio.to_thread(get_video_formats_info, url)
            if not qualities:
                await status.edit_text(get_text(lang, "no_qualities"))
                return

            keyboard = [[InlineKeyboardButton(f"🎬 {q['height']}p (~{q['size_mb']} МБ)", callback_data=f"vdl:{q['height']}")] for q in qualities]
            keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="back_to_format")])
            await status.edit_text(get_text(lang, "choose_quality").format(title=title[:60]), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
            
            increment_downloads(user_id, url)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_video(chat_id=query.message.chat_id, video=f, supports_streaming=True, duration=int(info.get("duration", 0)) or None)
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                await status.edit_text(get_text(lang, "file_too_large_video").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}"), parse_mode="Markdown")
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
            
            increment_downloads(user_id, url)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, title=info.get("title", "audio")[:64], performer=info.get("artist") or info.get("uploader"), duration=int(info.get("duration", 0)) or None)
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                await status.edit_text(get_text(lang, "file_too_large_audio").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}"), parse_mode="Markdown")
        except Exception as error:
            logger.exception("Download failed")
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return


async def handle_admin_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    state = context.user_data.get("admin_state")
    lang = get_user_lang(user_id)
    
    if state == "await_cookies" and update.message.document:
        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        content = file_bytes.decode('utf-8', errors='ignore')
        save_db_cookies(content)
        context.user_data["admin_state"] = None
        await update.message.reply_text(get_text(lang, "cookies_updated"))


async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, state: str, lang: str):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    if state == "await_cookies":
        save_db_cookies(text)
        context.user_data["admin_state"] = None
        await update.message.reply_text(get_text(lang, "cookies_updated"))
        
    elif state == "await_admin_id":
        if text.isdigit():
            new_admin_id = int(text)
            add_admin(new_admin_id, added_by=user_id)
            context.user_data["admin_state"] = None
            await update.message.reply_text(get_text(lang, "admin_admin_added"))
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_id"))
            
    elif state == "await_user_search":
        if text.isdigit():
            target_id = int(text)
            info = get_user_info(target_id)
            if not info:
                await update.message.reply_text(get_text(lang, "user_not_found"))
                return
            context.user_data["admin_state"] = None
            
            banned = info[5]
            profile_text = get_text(lang, "user_info_admin").format(
                name=info[3] or info[2] or "User", id=info[0], date=info[6] if len(info)>6 else "-", downloads=info[4] if len(info)>4 else 0,
                banned="🔴 Так" if banned else "🟢 Ні"
            )
            keyboard = [[InlineKeyboardButton(get_text(lang, "history_btn"), callback_data=f"usr_hist:{target_id}")]]
            if banned: keyboard[0].append(InlineKeyboardButton(get_text(lang, "unban_btn"), callback_data=f"usr_unban:{target_id}"))
            else: keyboard[0].append(InlineKeyboardButton(get_text(lang, "ban_btn"), callback_data=f"usr_ban:{target_id}"))
            keyboard.append([InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")])
            
            await update.message.reply_text(profile_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_id"))

    elif state == "await_channel_data":
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            add_sponsored_channel(parts[0], parts[1], parts[2])
            context.user_data["admin_state"] = None
            await update.message.reply_text(get_text(lang, "admin_channel_added").format(title=parts[1]), parse_mode="Markdown")
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_channel_format"))

    elif state.startswith("await_edit_channel:"):
        ch_id = state.split(":", 1)[1]
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            update_sponsored_channel(ch_id, parts[0], parts[1])
            context.user_data["admin_state"] = None
            await update.message.reply_text(get_text(lang, "sponsor_updated"))
        else:
            await update.message.reply_text("❌ Формат: `Назва Посилання`")


# =========================================================
# APPLICATION SETUP & LIFESPAN
# =========================================================

telegram_app = Application.builder().token(TOKEN).updater(None).build()

telegram_app.add_handler(CommandHandler("start", start))
# Видалено команди /settings та /admin, тепер все через master_text_handler

telegram_app.add_handler(MessageHandler(filters.Document.ALL, handle_admin_doc))
telegram_app.add_handler(MessageHandler(filters.TEXT, master_text_handler))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


async def keep_alive_ping():
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
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_bgutil_provider()
    await telegram_app.initialize()
    await telegram_app.start()
    
    await setup_bot_commands(telegram_app.bot)
    
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
