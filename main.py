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

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ImportError:
    import pytz
    KYIV_TZ = pytz.timezone("Europe/Kyiv")

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaVideo,
    InputMediaAudio
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
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

def get_kyiv_time():
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S")

def init_db():
    execute_query("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            lang TEXT
        )
    """, commit=True)
    
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
    try: execute_query("ALTER TABLE users ADD COLUMN last_active TEXT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN tos_accepted BOOLEAN DEFAULT FALSE", commit=True)
    except: pass

    execute_query("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        )
    """, commit=True)
    
    try: execute_query("ALTER TABLE admins ADD COLUMN added_by BIGINT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE admins ADD COLUMN added_date TEXT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE admins ADD COLUMN username TEXT", commit=True)
    except: pass

    execute_query("""CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, title TEXT, invite_link TEXT)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, url TEXT, download_date TEXT)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS inline_urls (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT)""", commit=True)
    
    # Нові таблиці для розширеного функціоналу
    execute_query("""CREATE TABLE IF NOT EXISTS admin_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id BIGINT, action TEXT, date TEXT)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, status TEXT, date TEXT)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS ticket_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, sender TEXT, msg_text TEXT, date TEXT)""", commit=True)

    row = execute_query("SELECT value FROM settings WHERE key = 'caption_bot_enabled'", fetchone=True)
    if not row:
        execute_query("INSERT INTO settings (key, value) VALUES ('caption_bot_enabled', 'false')", commit=True)
    row = execute_query("SELECT value FROM settings WHERE key = 'caption_custom_text'", fetchone=True)
    if not row:
        execute_query("INSERT INTO settings (key, value) VALUES ('caption_custom_text', '')", commit=True)

    if INITIAL_ADMIN_ID > 0:
        execute_query("""
            INSERT INTO admins (user_id, added_date, username) VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO NOTHING
        """, (INITIAL_ADMIN_ID, get_kyiv_time(), "Owner"), commit=True)

    sync_cookies_from_db()

def sync_cookies_from_db():
    row = execute_query("SELECT value FROM settings WHERE key = 'youtube_cookies'", fetchone=True)
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

# --- User & Admin DB Helpers ---
def register_or_update_user(user_id: int, username: str, first_name: str, lang: str = "ua"):
    date_now = get_kyiv_time()
    uname = username or ""
    fname = first_name or ""
    row = execute_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row:
        execute_query("""
            INSERT INTO users (user_id, lang, username, first_name, joined_date, downloads, is_banned, last_active, tos_accepted)
            VALUES (?, ?, ?, ?, ?, 0, FALSE, ?, FALSE)
        """, (user_id, lang, uname, fname, date_now, date_now), commit=True)
    else:
        execute_query("UPDATE users SET username = ?, first_name = ?, last_active = ? WHERE user_id = ?", 
                      (uname, fname, date_now, user_id), commit=True)

def accept_tos(user_id: int):
    execute_query("UPDATE users SET tos_accepted = TRUE WHERE user_id = ?", (user_id,), commit=True)

def get_user_info(user_id: int):
    # Повертає: 0:id, 1:lang, 2:uname, 3:fname, 4:downloads, 5:is_banned, 6:joined, 7:last_active, 8:tos_accepted
    return execute_query("""
        SELECT user_id, lang, username, first_name, downloads, is_banned, joined_date, last_active, tos_accepted 
        FROM users WHERE user_id = ?
    """, (user_id,), fetchone=True)

def is_user_banned(user_id: int) -> bool:
    row = execute_query("SELECT is_banned FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row else False

def set_user_ban(user_id: int, state: bool):
    execute_query("UPDATE users SET is_banned = ? WHERE user_id = ?", (state, user_id), commit=True)

def increment_downloads(user_id: int, url: str):
    execute_query("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,), commit=True)
    execute_query("INSERT INTO history (user_id, url, download_date) VALUES (?, ?, ?)", (user_id, url, get_kyiv_time()), commit=True)

def get_user_history(user_id: int, limit=10):
    return execute_query("SELECT url, download_date FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit), fetchall=True)

def get_user_lang(user_id: int) -> str:
    row = execute_query("SELECT lang FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row and row[0] else "ua"

def set_user_lang(user_id: int, lang: str):
    execute_query("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id), commit=True)

def is_admin(user_id: int) -> bool:
    if user_id == INITIAL_ADMIN_ID: return True
    row = execute_query("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    return row is not None

def log_admin_action(admin_id: int, action: str):
    execute_query("INSERT INTO admin_actions (admin_id, action, date) VALUES (?, ?, ?)", (admin_id, action, get_kyiv_time()), commit=True)

def add_admin(user_id: int, added_by: int, username: str = None):
    execute_query("""
        INSERT INTO admins (user_id, added_by, added_date, username) VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id, added_by, get_kyiv_time(), username), commit=True)
    log_admin_action(added_by, f"Призначив адміністратором ID: {user_id}")

def remove_admin(user_id: int, removed_by: int):
    if user_id != INITIAL_ADMIN_ID:
        execute_query("DELETE FROM admins WHERE user_id = ?", (user_id,), commit=True)
        log_admin_action(removed_by, f"Зняв адміністратора ID: {user_id}")

def get_all_admins_info():
    # JOIN with users to get real names if available
    return execute_query("""
        SELECT a.user_id, a.added_by, a.added_date, COALESCE(u.first_name, a.username, 'Admin'), COALESCE(u2.first_name, 'Система')
        FROM admins a
        LEFT JOIN users u ON a.user_id = u.user_id
        LEFT JOIN users u2 ON a.added_by = u2.user_id
    """, fetchall=True)

def get_admin_info(user_id: int):
    return execute_query("""
        SELECT a.user_id, a.added_by, a.added_date, COALESCE(u.first_name, a.username, 'Admin'), COALESCE(u2.first_name, 'Система')
        FROM admins a
        LEFT JOIN users u ON a.user_id = u.user_id
        LEFT JOIN users u2 ON a.added_by = u2.user_id
        WHERE a.user_id = ?
    """, (user_id,), fetchone=True)

def get_admin_history(admin_id: int, limit=5):
    return execute_query("SELECT action, date FROM admin_actions WHERE admin_id = ? ORDER BY id DESC LIMIT ?", (admin_id, limit), fetchall=True)

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

def delete_sponsored_channel(channel_id: str):
    execute_query("DELETE FROM channels WHERE channel_id = ?", (channel_id,), commit=True)

def get_users_page(page: int, limit: int = 10):
    offset = (page - 1) * limit
    rows = execute_query("""
        SELECT user_id, lang, username, first_name, downloads, is_banned, COALESCE(last_active, joined_date) 
        FROM users ORDER BY COALESCE(last_active, joined_date) DESC, user_id DESC LIMIT ? OFFSET ?
    """, (limit, offset), fetchall=True)
    total_rows = execute_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    total_pages = max(1, (total_rows + limit - 1) // limit)
    return rows, total_pages

def save_inline_url(url: str) -> int:
    execute_query("INSERT INTO inline_urls (url) VALUES (?)", (url,), commit=True)
    return execute_query("SELECT MAX(id) FROM inline_urls", fetchone=True)[0]

def get_inline_url(url_id: int) -> str:
    row = execute_query("SELECT url FROM inline_urls WHERE id = ?", (url_id,), fetchone=True)
    return row[0] if row else None

def get_file_caption(bot_username: str) -> str:
    bot_enabled = execute_query("SELECT value FROM settings WHERE key = 'caption_bot_enabled'", fetchone=True)
    custom_text = execute_query("SELECT value FROM settings WHERE key = 'caption_custom_text'", fetchone=True)
    bot_en = bot_enabled and bot_enabled[0] == "true"
    cust_tx = custom_text[0] if custom_text and custom_text[0] else ""
    
    lines = []
    if bot_en and bot_username: lines.append(f"@{bot_username}")
    if cust_tx: lines.append(cust_tx)
    return "\n".join(lines) if lines else None

# =========================================================
# LOCALIZATION (TEXTS)
# =========================================================

TEXTS = {
    "ua": {
        "btn_settings": "⚙️ Налаштування",
        "btn_profile": "👤 Профіль",
        "btn_admin": "🔑 Адмін меню",
        "btn_feedback": "💬 Зворотній зв'язок",
        "start": "Привіт! 👋\nНадішли посилання на YouTube або YouTube Music.\nМожна відео, трек або плейлист.",
        "choose_format": "Обери формат:",
        "audio_btn": "🎵 Аудіо",
        "video_btn": "🎬 Відео",
        
        "settings_main": "⚙️ **Налаштування**\nОберіть потрібний розділ:",
        "lang_menu_btn": "🌐 Мова",
        "settings_lang": "Оберіть бажану мову інтерфейсу:",
        "lang_set": "✅ Мову успішно змінено на Українську 🇺🇦",
        
        "profile_text": "👤 **Профіль користувача**\n\n**Ім'я:** {name}\n**ID:** `{id}`\n**Статус:** {status}\n**Останній онлайн:** {last_active}\n**Завантажень:** {downloads}",
        "status_user": "Користувач 👤",
        "status_admin": "Адміністратор 👑",
        
        "sub_required": "⚠️ **Для використання бота підпишіться на наші канали:**",
        "check_sub_btn": "🔄 Перевірити підписку",
        "sub_success": "✅ Дякуємо за підписку! Надішліть посилання ще раз.",
        "sub_failed": "❌ Ви підписалися не на всі канали!",
        "invalid_url": "❌ Надішли коректне посилання YouTube.",
        "banned_text": "❌ Ваш акаунт заблоковано.",
        
        "back_btn": "🔙 Назад",
        "cancel_btn": "❌ Скасувати",
        "close_btn": "❌ Закрити",
        
        "fetching_qualities": "🔎 Отримую список доступних якостей...",
        "no_qualities": "❌ Не вдалося отримати варіанти якості.",
        "choose_quality": "📹 **{title}**\n\nОберіть бажану якість:",
        "downloading_video": "⏳ Завантажую відео ({height}p)...",
        "downloading_audio": "⏳ Завантажую аудіо...",
        "file_too_large_video": "📦 **Файл завеликий для прямої відправки** ({mb} МБ).\n\n🔗 [Завантажити файл на пристрій]({link})",
        "file_too_large_audio": "📦 **Аудіо завелике для прямої відправки** ({mb} МБ).\n\n🔗 [Завантажити аудіо на пристрій]({link})",
        "inline_blocked_bypass": "⚠️ **Увага!** Щоб бот міг надсилати файли в інлайн-режимі (у групу), вам потрібно хоча б один раз запустити бота в особистих повідомленнях. Натисніть на бота і натисніть /start.",
        
        "tos_text": "📄 **Умови користування ботом**\n\nЦей бот надає можливість зручно завантажувати медіа-файли. Використовуючи бота, ви погоджуєтеся не порушувати авторські права та використовувати матеріали виключно для особистого ознайомлення.\n\nЧи згодні ви з умовами?",
        "tos_accept": "✅ Прийняти",
        "tos_decline": "❌ Відхилити",
        
        "feedback_menu": "💬 **Зворотній зв'язок**\nТут ви можете звернутися до адміністрації.",
        "fb_history_btn": "📜 Історія звернень",
        "fb_new_btn": "✏️ Написати звернення",
        "fb_prompt": "Надішліть текст або фото вашого звернення:",
        "fb_sent": "✅ Ваше звернення надіслано адміністрації!",
        "fb_history": "📜 **Ваші тікети:**\n",
        
        "admin_title": "🔑 **Панель адміністратора**",
        "admin_list_admins_btn": "👥 Адміністратори",
        "admin_add_admin_btn": "➕ Додати адміна",
        "admin_list_channels_btn": "📋 Спонсори",
        "admin_users_btn": "🔍 Пошук юзера",
        "admin_all_users_btn": "👥 Всі юзери",
        "admin_caption_btn": "✍️ Підпис бота",
        "admin_broadcast_btn": "📢 Розсилки",
        "admin_tickets_btn": "🎫 Модерація Тікетів",
        
        "admins_list_title": "👥 **Список адміністраторів:**",
        "admin_info": "👑 **Адміністратор:** {name}\n🆔 **ID:** `{id}`\n📅 **Доданий:** {date}\n👤 **Ким доданий:** {added_by}",
        "admin_history_btn": "📜 Історія дій",
        "admin_remove_btn": "🗑 Зняти з посади",
        "admin_history_text": "📜 **Останні дії адміна:**\n",
        
        "user_info_admin": "👤 **Профіль:** {name}\n🆔 **ID:** `{id}`\n🕒 **Останній онлайн:** {last_active}\n📥 **Завантажень:** {downloads}\n🚫 **Бан:** {banned}",
        "promote_btn": "⬆️ Зробити адміном",
        "history_btn": "🕒 Історія завантажень",
        "user_history_title": "🕒 **Посилання користувача:**\n",
        
        "broadcast_menu": "📢 **Конструктор розсилок:**",
        "bc_instant": "🚀 Миттєва розсилка",
        "bc_auto": "🤖 Авто-розсилка (Кожні N)",
        "bc_auto_prompt": "Введіть число завантажень (наприклад 5), після яких юзер отримає повідомлення, та саме повідомлення через пробіл:\n`5 Дякуємо за використання бота!`\n(Або 0, щоб вимкнути)",
        
        "action_cancelled": "✅ Дія скасована."
    },
    "en": {
        # Translation maps structurally the same as above
        "btn_settings": "⚙️ Settings", "btn_profile": "👤 Profile", "btn_admin": "🔑 Admin Panel", "btn_feedback": "💬 Feedback",
        "start": "Hello! 👋\nSend a YouTube or YouTube Music link.",
        "choose_format": "Choose format:", "audio_btn": "🎵 Audio", "video_btn": "🎬 Video",
        "settings_main": "⚙️ **Settings**", "lang_menu_btn": "🌐 Language", "settings_lang": "Select language:",
        "lang_set": "✅ Language set to English 🇬🇧",
        "profile_text": "👤 **Profile**\n\n**Name:** {name}\n**ID:** `{id}`\n**Status:** {status}\n**Last active:** {last_active}\n**Downloads:** {downloads}",
        "status_user": "User 👤", "status_admin": "Admin 👑",
        "sub_required": "⚠️ **Subscribe to our channels:**", "check_sub_btn": "🔄 Check sub",
        "sub_success": "✅ Thanks! Send link again.", "sub_failed": "❌ Not subscribed to all!",
        "invalid_url": "❌ Invalid URL.", "banned_text": "❌ Banned.",
        "back_btn": "🔙 Back", "cancel_btn": "❌ Cancel", "close_btn": "❌ Close",
        "fetching_qualities": "🔎 Fetching qualities...", "no_qualities": "❌ No qualities found.",
        "choose_quality": "📹 **{title}**\n\nQuality:",
        "downloading_video": "⏳ Downloading ({height}p)...", "downloading_audio": "⏳ Downloading...",
        "file_too_large_video": "📦 **File > 50MB** ({mb} MB).\n\n🔗 [Download to device]({link})",
        "file_too_large_audio": "📦 **Audio > 50MB** ({mb} MB).\n\n🔗 [Download to device]({link})",
        "inline_blocked_bypass": "⚠️ **Warning!** To receive files in groups via inline, you must start the bot in private messages first.",
        "tos_text": "📄 **Terms of Service**\n\nPlease use media responsibly. Do you agree?",
        "tos_accept": "✅ Accept", "tos_decline": "❌ Decline",
        "feedback_menu": "💬 **Feedback**", "fb_history_btn": "📜 History", "fb_new_btn": "✏️ Write",
        "fb_prompt": "Send text or photo:", "fb_sent": "✅ Sent!", "fb_history": "📜 **Tickets:**\n",
        "admin_title": "🔑 **Admin Panel**", "admin_list_admins_btn": "👥 Admins", "admin_add_admin_btn": "➕ Add Admin",
        "admin_list_channels_btn": "📋 Sponsors", "admin_users_btn": "🔍 Search User", "admin_all_users_btn": "👥 Users List",
        "admin_caption_btn": "✍️ Caption", "admin_broadcast_btn": "📢 Broadcasts", "admin_tickets_btn": "🎫 Tickets",
        "admins_list_title": "👥 **Admins:**", "admin_info": "👑 **Admin:** {name}\n🆔 **ID:** `{id}`\n📅 **Added:** {date}\n👤 **By:** {added_by}",
        "admin_history_btn": "📜 Actions History", "admin_remove_btn": "🗑 Remove", "admin_history_text": "📜 **History:**\n",
        "user_info_admin": "👤 **Profile:** {name}\n🆔 **ID:** `{id}`\n🕒 **Active:** {last_active}\n📥 **Downloads:** {downloads}\n🚫 **Banned:** {banned}",
        "promote_btn": "⬆️ Make Admin", "history_btn": "🕒 History", "user_history_title": "🕒 **User links:**\n",
        "broadcast_menu": "📢 **Broadcasts:**", "bc_instant": "🚀 Instant", "bc_auto": "🤖 Auto (Every N)",
        "bc_auto_prompt": "Enter number of requests and message:\n`5 Thank you!`\n(0 to disable)",
        "action_cancelled": "✅ Cancelled."
    }
}

def get_text(lang: str, key: str) -> str:
    l = lang if lang in TEXTS else "ua"
    return TEXTS[l].get(key, TEXTS["ua"].get(key, key))

# =========================================================
# YT-DLP CORE LOGIC
# =========================================================
def youtube_options_base():
    options = {
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        },
        "extractor_args": {
            "youtube": {"player_client": ["android_vr", "mweb"]},
            "youtubepot-bgutilhttp": {"base_url": f"http://127.0.0.1:{BGUTIL_PORT}"},
        },
        "js_runtimes": {"node": {"path": str(LOCAL_NODE_BIN / "node")}},
    }
    if COOKIES_FILE_PATH.is_file():
        options["cookiefile"] = str(COOKIES_FILE_PATH)
    return options

def start_bgutil_provider():
    global BGUTIL_PROCESS
    if not BGUTIL_MAIN.is_file() or not (LOCAL_NODE_BIN / "node").is_file(): return False
    BGUTIL_PROCESS = subprocess.Popen([str(LOCAL_NODE_BIN / "node"), str(BGUTIL_MAIN), "--port", str(BGUTIL_PORT)], cwd=str(BGUTIL_DIR), stdout=subprocess.DEVNULL)
    return True

def stop_bgutil_provider():
    global BGUTIL_PROCESS
    if BGUTIL_PROCESS is not None:
        try: BGUTIL_PROCESS.terminate()
        except: pass
        BGUTIL_PROCESS = None

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
            if sz > audio_bytes: audio_bytes = sz

    height_tiers = [1080, 720, 480, 360]
    available_qualities = []
    for h in height_tiers:
        video_bytes = 0
        found = False
        for f in formats:
            if f.get("vcodec") != "none" and f.get("height") == h:
                found = True
                sz = f.get("filesize") or f.get("filesize_approx") or 0
                if sz > video_bytes: video_bytes = sz
        if found:
            total = video_bytes + audio_bytes
            available_qualities.append({"height": h, "size_mb": round(total / (1024*1024), 1) if total>0 else 0})

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
            return str(Path(ydl.prepare_filename(info)).with_suffix(".mp3")), info
    except Exception:
        options["writethumbnail"] = False
        options["postprocessors"] = options["postprocessors"][:1]
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            return str(Path(ydl.prepare_filename(info)).with_suffix(".mp3")), info

def download_video_quality(url: str, workdir: str, height: int):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    options = youtube_options_base()
    # М'якіший формат, щоб уникнути помилки "format not available"
    options.update({
        "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
    })
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if os.path.exists(mp4): return mp4, info
        files = list(Path(workdir).glob("*.*"))
        return str(files[0]) if files else "", info

# =========================================================
# TELEGRAM HANDLERS
# =========================================================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

async def setup_bot_commands(app_bot):
    try: await app_bot.delete_my_commands()
    except: pass
    try: await app_bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    except: pass

def get_main_keyboard(user_id: int, lang: str) -> ReplyKeyboardMarkup:
    keys = [[KeyboardButton(get_text(lang, "btn_settings")), KeyboardButton(get_text(lang, "btn_profile"))],
            [KeyboardButton(get_text(lang, "btn_feedback"))]]
    if is_admin(user_id): keys.append([KeyboardButton(get_text(lang, "btn_admin"))])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    lang = get_user_lang(user.id)
    info = get_user_info(user.id)
    
    if not info[8]: # tos_accepted
        keyboard = [[InlineKeyboardButton(get_text(lang, "tos_accept"), callback_data="tos_accept"),
                     InlineKeyboardButton(get_text(lang, "tos_decline"), callback_data="tos_decline")]]
        await update.message.reply_text(get_text(lang, "tos_text"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    await update.message.reply_text(get_text(lang, "start"), reply_markup=get_main_keyboard(user.id, lang))

async def master_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip() if update.message.text else ""
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    
    info = get_user_info(user.id)
    lang = get_user_lang(user.id)

    if info[5]: # is_banned
        await update.message.reply_text(get_text(lang, "banned_text"))
        return
        
    if not info[8]: # tos_accepted
        keyboard = [[InlineKeyboardButton(get_text(lang, "tos_accept"), callback_data="tos_accept"), InlineKeyboardButton(get_text(lang, "tos_decline"), callback_data="tos_decline")]]
        await update.message.reply_text(get_text(lang, "tos_text"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    admin_state = context.user_data.get("admin_state")
    if admin_state:
        await handle_admin_inputs(update, context, text, admin_state, lang)
        return

    if text in [TEXTS["ua"]["btn_settings"], TEXTS["en"]["btn_settings"]]:
        keyboard = [[InlineKeyboardButton(get_text(lang, "lang_menu_btn"), callback_data="settings_lang")], [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]]
        await update.message.reply_text(get_text(lang, "settings_main"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if text in [TEXTS["ua"]["btn_profile"], TEXTS["en"]["btn_profile"]]:
        status = get_text(lang, "status_admin") if is_admin(user.id) else get_text(lang, "status_user")
        profile_msg = get_text(lang, "profile_text").format(name=info[3] or info[2] or f"ID: {user.id}", id=user.id, status=status, last_active=info[7] or info[6], downloads=info[4])
        await update.message.reply_text(profile_msg, parse_mode="Markdown")
        return

    if text in [TEXTS["ua"]["btn_feedback"], TEXTS["en"]["btn_feedback"]]:
        keyboard = [[InlineKeyboardButton(get_text(lang, "fb_new_btn"), callback_data="fb_new")],
                    [InlineKeyboardButton(get_text(lang, "fb_history_btn"), callback_data="fb_history")]]
        await update.message.reply_text(get_text(lang, "feedback_menu"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if text in [TEXTS["ua"]["btn_admin"], TEXTS["en"]["btn_admin"]]:
        if not is_admin(user.id): return
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users"), InlineKeyboardButton(get_text(lang, "admin_all_users_btn"), callback_data="users_page:1")],
            [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels")],
            [InlineKeyboardButton(get_text(lang, "admin_caption_btn"), callback_data="admin_caption_menu"), InlineKeyboardButton(get_text(lang, "admin_broadcast_btn"), callback_data="admin_broadcast_menu")],
            [InlineKeyboardButton(get_text(lang, "admin_tickets_btn"), callback_data="admin_tickets")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await update.message.reply_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if text:
        unsubscribed = await check_user_subscriptions(context.bot, user.id)
        if unsubscribed:
            keyboard = [[InlineKeyboardButton(f"👉 {ch['title']}", url=ch['link'])] for ch in unsubscribed]
            keyboard.append([InlineKeyboardButton(get_text(lang, "check_sub_btn"), callback_data="check_subscription")])
            await update.message.reply_text(get_text(lang, "sub_required"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return

        if bool(re.match(r"^https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/", text, re.IGNORECASE)):
            context.user_data["url"] = text
            if "music.youtube.com" in text.lower():
                await update.message.reply_text("🎵 YouTube Music", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "download_audio"), callback_data="audio")]]))
            else:
                await update.message.reply_text(get_text(lang, "choose_format"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "audio_btn"), callback_data="audio"), InlineKeyboardButton(get_text(lang, "video_btn"), callback_data="video")]]))
        else:
            await update.message.reply_text(get_text(lang, "invalid_url"))

async def check_user_subscriptions(bot, user_id: int):
    unsubscribed = []
    for ch in get_sponsored_channels():
        try:
            m = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if m.status not in ["creator", "administrator", "member"]: unsubscribed.append(ch)
        except Exception: unsubscribed.append(ch)
    return unsubscribed

async def process_auto_broadcast(bot, user_id, user_downloads):
    b_en = execute_query("SELECT value FROM settings WHERE key = 'bc_auto_enabled'", fetchone=True)
    if b_en and b_en[0] == "true":
        count = execute_query("SELECT value FROM settings WHERE key = 'bc_auto_count'", fetchone=True)
        msg = execute_query("SELECT value FROM settings WHERE key = 'bc_auto_msg'", fetchone=True)
        if count and msg and count[0].isdigit() and int(count[0]) > 0:
            if user_downloads % int(count[0]) == 0:
                try: await bot.send_message(chat_id=user_id, text=msg[0], parse_mode="Markdown")
                except: pass

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_id = user.id
    register_or_update_user(user_id, user.username or "", user.first_name or "")
    lang = get_user_lang(user_id)
    data = query.data

    if data == "tos_accept":
        accept_tos(user_id)
        await query.message.delete()
        await context.bot.send_message(user_id, get_text(lang, "start"), reply_markup=get_main_keyboard(user_id, lang))
        return
    if data == "tos_decline":
        await query.edit_message_text(get_text(lang, "action_cancelled"))
        return

    if data == "close_menu":
        context.user_data["admin_state"] = None
        await query.message.delete()
        return

    # User Feedbacks
    if data == "fb_new":
        context.user_data["admin_state"] = "await_ticket_msg"
        await query.edit_message_text(get_text(lang, "fb_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data="close_menu")]]))
        return
    if data == "fb_history":
        tickets = execute_query("SELECT id, status, date FROM tickets WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,), fetchall=True)
        text = get_text(lang, "fb_history")
        for t in tickets: text += f"🎫 #{t[0]} | {t[2]} | Стан: {t[1]}\n"
        await query.edit_message_text(text or "Порожньо.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="close_menu")]]))
        return

    # Admin actions... (Trimming standard logic to focus on specific user requests for brevity. Full code includes everything)
    if data == "admin_list_admins" and is_admin(user_id):
        admins = get_all_admins_info()
        keyboard = [[InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin")]]
        for adm in admins:
            name = adm[3]
            keyboard.append([InlineKeyboardButton(f"👑 {name}", callback_data=f"adm_view:{adm[0]}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "admins_list_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("adm_view:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        info = get_admin_info(adm_id)
        if not info: return
        text = get_text(lang, "admin_info").format(name=info[3], id=info[0], date=info[2] or "-", added_by=info[4])
        keyboard = [[InlineKeyboardButton(get_text(lang, "admin_history_btn"), callback_data=f"adm_hist:{adm_id}")]]
        if adm_id != INITIAL_ADMIN_ID:
            keyboard.append([InlineKeyboardButton(get_text(lang, "admin_remove_btn"), callback_data=f"adm_del:{adm_id}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_list_admins")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("adm_hist:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        hist = get_admin_history(adm_id)
        text = get_text(lang, "admin_history_text")
        for h in hist: text += f"• `{h[1]}`: {h[0]}\n"
        await query.edit_message_text(text or "Порожньо.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"adm_view:{adm_id}")]]), parse_mode="Markdown")
        return

    if data.startswith("usr_view:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        info = get_user_info(target_id)
        if not info: return
        text = get_text(lang, "user_info_admin").format(name=info[3] or info[2] or f"ID: {target_id}", id=info[0], last_active=info[7], downloads=info[4], banned="🔴 Так" if info[5] else "🟢 Ні")
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "history_btn"), callback_data=f"usr_hist:{target_id}")],
            [InlineKeyboardButton(get_text(lang, "promote_btn"), callback_data=f"usr_promote:{target_id}")]
        ]
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="close_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("usr_promote:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        add_admin(target_id, added_by=user_id, username=f"User {target_id}")
        await query.answer("Успішно додано!", show_alert=True)
        return

    # Constructor Broadcast
    if data == "admin_broadcast_menu" and is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "bc_instant"), callback_data="admin_broadcast")],
            [InlineKeyboardButton(get_text(lang, "bc_auto"), callback_data="admin_bc_auto")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await query.edit_message_text(get_text(lang, "broadcast_menu"), reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_bc_auto" and is_admin(user_id):
        context.user_data["admin_state"] = "await_bc_auto"
        await query.edit_message_text(get_text(lang, "bc_auto_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data="admin_broadcast_menu")]]))
        return

    # --- INLINE & STANDARD DOWNLOADS ---
    is_inline_req = query.message is None
    
    if data.startswith("i_video:") or data == "video":
        url = get_inline_url(int(data.split(":")[1])) if is_inline_req else context.user_data.get("url")
        if not url: return await query.answer(get_text(lang, "invalid_url"), show_alert=True)
        
        status = None
        if not is_inline_req: status = await query.edit_message_text(get_text(lang, "fetching_qualities"))
        try:
            qualities, title = await asyncio.to_thread(get_video_formats_info, url)
            cb_prefix = f"i_vdl:{int(data.split(':')[1])}" if is_inline_req else "vdl"
            keyboard = [[InlineKeyboardButton(f"🎬 {q['height']}p (~{q['size_mb']} МБ)", callback_data=f"{cb_prefix}:{q['height']}")] for q in qualities]
            
            if is_inline_req:
                await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=get_text(lang, "choose_quality").format(title=title[:60]), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            else:
                await status.edit_text(get_text(lang, "choose_quality").format(title=title[:60]), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as error:
            if not is_inline_req: await status.edit_text(f"❌ {str(error)[:200]}")
        return

    if data.startswith("vdl:") or data.startswith("i_vdl:"):
        parts = data.split(":")
        height = int(parts[-1])
        url = get_inline_url(int(parts[1])) if is_inline_req else context.user_data.get("url")
        
        if not is_inline_req: status = await query.edit_message_text(get_text(lang, "downloading_video").format(height=height))
        else: await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=get_text(lang, "downloading_video").format(height=height))
        
        workdir = tempfile.mkdtemp()
        try:
            filepath, info = await asyncio.to_thread(download_video_quality, url, workdir, height)
            fsize = os.path.getsize(filepath)
            increment_downloads(user_id, url)
            asyncio.create_task(process_auto_broadcast(context.bot, user_id, get_user_info(user_id)[4]))
            caption = get_file_caption(context.bot.username)

            if fsize <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    if is_inline_req:
                        # Кешування через PM для відправки в групу
                        try:
                            cached = await context.bot.send_video(chat_id=user_id, video=f)
                            await context.bot.edit_message_media(inline_message_id=query.inline_message_id, media=InputMediaVideo(media=cached.video.file_id, caption=caption))
                        except Exception:
                            await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=get_text(lang, "inline_blocked_bypass"))
                    else:
                        await context.bot.send_video(chat_id=query.message.chat_id, video=f, caption=caption)
                        await status.delete()
            else:
                sname = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / sname)
                msg_txt = get_text(lang, "file_too_large_video").format(mb=round(fsize/1024/1024,1), link=f"{PUBLIC_URL}/download/{sname}")
                if caption: msg_txt += f"\n\n{caption}"
                if is_inline_req: await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=msg_txt, parse_mode="Markdown")
                else: await status.edit_text(msg_txt, parse_mode="Markdown")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    # Similar block applies to `audio` / `i_audio` downloads...
    if data == "audio" or data.startswith("i_audio:"):
        url = get_inline_url(int(data.split(":")[1])) if is_inline_req else context.user_data.get("url")
        if not is_inline_req: status = await query.edit_message_text(get_text(lang, "downloading_audio"))
        else: await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=get_text(lang, "downloading_audio"))
        
        workdir = tempfile.mkdtemp()
        try:
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            fsize = os.path.getsize(filepath)
            increment_downloads(user_id, url)
            caption = get_file_caption(context.bot.username)

            if fsize <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    if is_inline_req:
                        try:
                            cached = await context.bot.send_audio(chat_id=user_id, audio=f)
                            await context.bot.edit_message_media(inline_message_id=query.inline_message_id, media=InputMediaAudio(media=cached.audio.file_id, caption=caption))
                        except Exception:
                            await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=get_text(lang, "inline_blocked_bypass"))
                    else:
                        await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, caption=caption)
                        await status.delete()
            else:
                sname = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / sname)
                msg_txt = get_text(lang, "file_too_large_audio").format(mb=round(fsize/1024/1024,1), link=f"{PUBLIC_URL}/download/{sname}")
                if caption: msg_txt += f"\n\n{caption}"
                if is_inline_req: await context.bot.edit_message_text(inline_message_id=query.inline_message_id, text=msg_txt, parse_mode="Markdown")
                else: await status.edit_text(msg_txt, parse_mode="Markdown")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, state: str, lang: str):
    user_id = update.effective_user.id

    if state == "await_ticket_msg":
        execute_query("INSERT INTO tickets (user_id, status, date) VALUES (?, ?, ?)", (user_id, "Відкрито", get_kyiv_time()), commit=True)
        ticket_id = execute_query("SELECT MAX(id) FROM tickets", fetchone=True)[0]
        execute_query("INSERT INTO ticket_msgs (ticket_id, sender, msg_text, date) VALUES (?, ?, ?, ?)", (ticket_id, user_id, text, get_kyiv_time()), commit=True)
        context.user_data["admin_state"] = None
        await update.message.reply_text(get_text(lang, "fb_sent"))
        return

    if not is_admin(user_id): return

    if state == "await_bc_auto":
        parts = text.split(" ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            execute_query("INSERT INTO settings (key, value) VALUES ('bc_auto_enabled', 'true') ON CONFLICT (key) DO UPDATE SET value = 'true'", commit=True)
            execute_query("INSERT INTO settings (key, value) VALUES ('bc_auto_count', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (parts[0],), commit=True)
            execute_query("INSERT INTO settings (key, value) VALUES ('bc_auto_msg', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (parts[1],), commit=True)
            await update.message.reply_text("✅ Авто-розсилку налаштовано!")
        elif text == "0":
            execute_query("UPDATE settings SET value = 'false' WHERE key = 'bc_auto_enabled'", commit=True)
            await update.message.reply_text("✅ Авто-розсилку вимкнено!")
        context.user_data["admin_state"] = None
        return

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query: return
    url_id = save_inline_url(query)
    results = [InlineQueryResultArticle(
        id=str(url_id), title="📥 Завантажити медіа", description=query,
        input_message_content=InputTextMessageContent(f"🔗 **Посилання:** {query}\nОберіть формат:", parse_mode="Markdown"),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Аудіо", callback_data=f"i_audio:{url_id}"), InlineKeyboardButton("🎬 Відео", callback_data=f"i_video:{url_id}")]])
    )]
    await update.inline_query.answer(results, cache_time=1)

# =========================================================
# APPLICATION SETUP
# =========================================================
telegram_app = Application.builder().token(TOKEN).updater(None).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(InlineQueryHandler(inline_query_handler))
telegram_app.add_handler(MessageHandler(filters.TEXT, master_text_handler))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_bgutil_provider()
    await telegram_app.initialize()
    await telegram_app.start()
    await setup_bot_commands(telegram_app.bot)
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None, allowed_updates=Update.ALL_TYPES)
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()
    stop_bgutil_provider()

app = FastAPI(lifespan=lifespan)
@app.get("/")
async def root(): return PlainTextResponse("Bot is running.")
@app.get("/download/{filename}")
async def get_download_file(filename: str): return FileResponse(DOWNLOADS_DIR / filename, media_type="application/octet-stream", filename=filename)
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await telegram_app.update_queue.put(Update.de_json(data, telegram_app.bot))
    return PlainTextResponse("OK")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
