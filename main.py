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
from zoneinfo import ZoneInfo

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
    InputMediaAudio,
    InputMediaVideo,
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

KYIV_TZ = ZoneInfo("Europe/Kyiv")

def get_kyiv_now_str() -> str:
    return datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")


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
    execute_query("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            lang TEXT,
            username TEXT,
            first_name TEXT,
            downloads INTEGER DEFAULT 0,
            is_banned BOOLEAN DEFAULT FALSE,
            joined_date TEXT,
            last_active TEXT,
            terms_accepted BOOLEAN DEFAULT FALSE
        )
    """, commit=True)
    
    for col, ctype in [
        ("username", "TEXT"),
        ("first_name", "TEXT"),
        ("downloads", "INTEGER DEFAULT 0"),
        ("is_banned", "BOOLEAN DEFAULT FALSE"),
        ("joined_date", "TEXT"),
        ("last_active", "TEXT"),
        ("terms_accepted", "BOOLEAN DEFAULT FALSE")
    ]:
        try: execute_query(f"ALTER TABLE users ADD COLUMN {col} {ctype}", commit=True)
        except Exception: pass

    execute_query("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            added_date TEXT,
            username TEXT
        )
    """, commit=True)
    
    for col, ctype in [
        ("added_by", "BIGINT"),
        ("added_date", "TEXT"),
        ("username", "TEXT")
    ]:
        try: execute_query(f"ALTER TABLE admins ADD COLUMN {col} {ctype}", commit=True)
        except Exception: pass

    execute_query("""
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id BIGINT,
            action TEXT,
            created_at TEXT
        )
    """, commit=True)

    execute_query("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            invite_link TEXT
        )
    """, commit=True)

    execute_query("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """, commit=True)
    
    execute_query("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT,
            url TEXT,
            download_date TEXT
        )
    """, commit=True)

    execute_query("""
        CREATE TABLE IF NOT EXISTS inline_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT
        )
    """, commit=True)

    execute_query("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """, commit=True)

    execute_query("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            sender TEXT,
            message_type TEXT,
            text TEXT,
            file_id TEXT,
            created_at TEXT
        )
    """, commit=True)

    execute_query("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            scheduled_at TEXT,
            trigger_count INTEGER,
            chat_id BIGINT,
            message_id INTEGER,
            status TEXT,
            created_at TEXT
        )
    """, commit=True)

    default_settings = [
        ('caption_bot_enabled', 'false'),
        ('caption_custom_text', ''),
        ('maintenance_mode', 'false')
    ]
    for key, val in default_settings:
        row = execute_query("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)
        if not row:
            execute_query("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val), commit=True)

    if INITIAL_ADMIN_ID > 0:
        date_now = get_kyiv_now_str()
        execute_query("""
            INSERT INTO admins (user_id, added_by, added_date, username) VALUES (?, 0, ?, 'Owner')
            ON CONFLICT (user_id) DO NOTHING
        """, (INITIAL_ADMIN_ID, date_now), commit=True)

    sync_cookies_from_db()


# --- User & Admin Helpers ---

def register_or_update_user(user_id: int, username: str, first_name: str, lang: str = "ua"):
    date_now = get_kyiv_now_str()
    uname = username or ""
    fname = first_name or ""
    row = execute_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row:
        execute_query("""
            INSERT INTO users (user_id, lang, username, first_name, joined_date, downloads, is_banned, last_active, terms_accepted)
            VALUES (?, ?, ?, ?, ?, 0, FALSE, ?, FALSE)
        """, (user_id, lang, uname, fname, date_now, date_now), commit=True)
    else:
        execute_query("UPDATE users SET username = ?, first_name = ?, last_active = ? WHERE user_id = ?", 
                      (uname, fname, date_now, user_id), commit=True)

def check_terms_accepted(user_id: int) -> bool:
    row = execute_query("SELECT terms_accepted FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return bool(row[0]) if row and row[0] is not None else False

def set_terms_accepted(user_id: int, state: bool = True):
    execute_query("UPDATE users SET terms_accepted = ? WHERE user_id = ?", (state, user_id), commit=True)

def get_user_info(user_id: int):
    return execute_query("""
        SELECT user_id, lang, username, first_name, downloads, is_banned, joined_date, last_active, terms_accepted 
        FROM users WHERE user_id = ?
    """, (user_id,), fetchone=True)

def get_user_display_name(user_id: int) -> str:
    if not user_id or user_id == 0:
        return "Система"
    info = get_user_info(user_id)
    if info:
        if info[3]: return info[3]
        if info[2]: return f"@{info[2]}"
    return f"ID: {user_id}"

def is_user_banned(user_id: int) -> bool:
    row = execute_query("SELECT is_banned FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row else False

def set_user_ban(user_id: int, state: bool):
    execute_query("UPDATE users SET is_banned = ? WHERE user_id = ?", (state, user_id), commit=True)

def increment_downloads(user_id: int, url: str, bot=None):
    execute_query("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,), commit=True)
    date_now = get_kyiv_now_str()
    execute_query("INSERT INTO history (user_id, url, download_date) VALUES (?, ?, ?)", (user_id, url, date_now), commit=True)
    
    if bot:
        row = execute_query("SELECT downloads FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        if row:
            user_dl_count = row[0]
            triggers = execute_query("SELECT chat_id, message_id, trigger_count FROM broadcasts WHERE type = 'trigger' AND status = 'active'", fetchall=True)
            for tr in triggers:
                chat_id, msg_id, tr_count = tr
                if tr_count > 0 and user_dl_count % tr_count == 0:
                    asyncio.create_task(send_trigger_broadcast(bot, user_id, chat_id, msg_id))

async def send_trigger_broadcast(bot, user_id: int, from_chat_id: int, message_id: int):
    try:
        await bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
    except Exception as e:
        logger.warning("Trigger broadcast failed for user %s: %s", user_id, e)

def get_user_history(user_id: int, limit=20):
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
    date_now = get_kyiv_now_str()
    uname = username or get_user_display_name(user_id)
    execute_query("""
        INSERT INTO admins (user_id, added_by, added_date, username) VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET added_by = excluded.added_by, added_date = excluded.added_date, username = excluded.username
    """, (user_id, added_by, date_now, uname), commit=True)
    log_admin_action(added_by, f"Додано адміна ID: {user_id}")

def remove_admin(user_id: int, removed_by: int = 0):
    if user_id != INITIAL_ADMIN_ID:
        execute_query("DELETE FROM admins WHERE user_id = ?", (user_id,), commit=True)
        if removed_by:
            log_admin_action(removed_by, f"Видалено адміна ID: {user_id}")

def get_all_admins_info():
    return execute_query("SELECT user_id, added_by, added_date, username FROM admins ORDER BY user_id ASC", fetchall=True)

def get_admin_info(user_id: int):
    return execute_query("SELECT user_id, added_by, added_date, username FROM admins WHERE user_id = ?", (user_id,), fetchone=True)

def log_admin_action(admin_id: int, action: str):
    date_now = get_kyiv_now_str()
    execute_query("INSERT INTO admin_logs (admin_id, action, created_at) VALUES (?, ?, ?)", (admin_id, action, date_now), commit=True)

def get_admin_logs(admin_id: int, limit: int = 15):
    return execute_query("SELECT action, created_at FROM admin_logs WHERE admin_id = ? ORDER BY id DESC LIMIT ?", (admin_id, limit), fetchall=True)

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

def get_users_page(page: int, limit: int = 10):
    offset = (page - 1) * limit
    rows = execute_query("""
        SELECT user_id, lang, username, first_name, downloads, is_banned, COALESCE(last_active, joined_date) 
        FROM users 
        ORDER BY COALESCE(last_active, joined_date) DESC, user_id DESC LIMIT ? OFFSET ?
    """, (limit, offset), fetchall=True)
    total_rows = execute_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    total_pages = max(1, (total_rows + limit - 1) // limit)
    return rows, total_pages

def save_inline_url(url: str) -> int:
    execute_query("INSERT INTO inline_urls (url) VALUES (?)", (url,), commit=True)
    row = execute_query("SELECT MAX(id) FROM inline_urls", fetchone=True)
    return row[0] if row else 1

def get_inline_url(url_id: int) -> str:
    row = execute_query("SELECT url FROM inline_urls WHERE id = ?", (url_id,), fetchone=True)
    return row[0] if row else None

def get_file_caption(bot_username: str) -> str:
    bot_enabled_row = execute_query("SELECT value FROM settings WHERE key = 'caption_bot_enabled'", fetchone=True)
    custom_row = execute_query("SELECT value FROM settings WHERE key = 'caption_custom_text'", fetchone=True)
    
    bot_enabled = bot_enabled_row and bot_enabled_row[0] == "true"
    custom_text = custom_row[0] if custom_row and custom_row[0] else ""
    
    lines = []
    if bot_enabled and bot_username:
        lines.append(f"@{bot_username}")
    if custom_text:
        if lines: lines.append("")
        lines.append(custom_text)
        
    return "\n".join(lines) if lines else None

# --- Ticket Helpers ---

def create_ticket(user_id: int) -> int:
    now_str = get_kyiv_now_str()
    execute_query("INSERT INTO tickets (user_id, status, created_at, updated_at) VALUES (?, 'open', ?, ?)", (user_id, now_str, now_str), commit=True)
    row = execute_query("SELECT MAX(id) FROM tickets WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row else 1

def add_ticket_message(ticket_id: int, sender: str, msg_type: str, text: str = "", file_id: str = ""):
    now_str = get_kyiv_now_str()
    execute_query("INSERT INTO ticket_messages (ticket_id, sender, message_type, text, file_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (ticket_id, sender, msg_type, text, file_id, now_str), commit=True)
    execute_query("UPDATE tickets SET updated_at = ? WHERE id = ?", (now_str, ticket_id), commit=True)

def get_user_tickets(user_id: int):
    return execute_query("SELECT id, status, created_at, updated_at FROM tickets WHERE user_id = ? ORDER BY id DESC", (user_id,), fetchall=True)

def get_ticket_info(ticket_id: int):
    return execute_query("SELECT id, user_id, status, created_at, updated_at FROM tickets WHERE id = ?", (ticket_id,), fetchone=True)

def get_ticket_messages(ticket_id: int):
    return execute_query("SELECT id, sender, message_type, text, file_id, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,), fetchall=True)

def update_ticket_status(ticket_id: int, status: str):
    now_str = get_kyiv_now_str()
    execute_query("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (status, now_str, ticket_id), commit=True)

def get_all_tickets(status_filter: str = None):
    if status_filter:
        return execute_query("SELECT id, user_id, status, created_at, updated_at FROM tickets WHERE status = ? ORDER BY id DESC", (status_filter,), fetchall=True)
    return execute_query("SELECT id, user_id, status, created_at, updated_at FROM tickets ORDER BY id DESC", fetchall=True)


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
        "download_audio": "🎵 Завантажити аудіо",
        
        "terms_title": "📜 **Умови користування ботом**\n\nБудь ласка, ознайомтеся з умовами та підтвердіть свою згоду:\n1. Бот призначений для особистого завантаження медіафайлів.\n2. Користувач зобов'язується не використовувати бот у протиправних цілях.\n3. Адміністрація не відповідає за контент, завантажений користувачем.",
        "terms_accept_btn": "✅ Прийняти",
        "terms_decline_btn": "❌ Відхилити",
        "terms_declined_msg": "⚠️ Ви відхилили умови користування. Ви не можете користуватись ботом, поки не прийняли умови.",
        "terms_accepted_msg": "✅ Дякуємо! Ви успішно прийняли умови користування.",

        "settings_main": "⚙️ **Налаштування**\nОберіть потрібний розділ:",
        "lang_menu_btn": "🌐 Мова",
        "settings_lang": "Оберіть бажану мову інтерфейсу:",
        "lang_set": "✅ Мову успішно змінено на Українську 🇺🇦",
        
        "profile_text": "👤 **Профіль користувача**\n\n**Ім'я:** {name}\n**ID:** `{id}`\n**Статус:** {status}\n**Останній онлайн (Київ):** {last_active}\n**Завантажень:** {downloads}",
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
        
        "admin_title": "🔑 **Панель адміністратора**",
        "admin_list_admins_btn": "👥 Список адмінів",
        "admin_add_admin_btn": "➕ Додати адміна",
        "admin_add_channel_btn": "📢 Додати спонсора",
        "admin_list_channels_btn": "📋 Список спонсорів",
        "admin_users_btn": "🔍 Пошук користувача",
        "admin_all_users_btn": "👥 Список учасників",
        "admin_caption_btn": "✍️ Додати підпис",
        "admin_broadcast_btn": "📢 Конструктор розсилки",
        "admin_cookies_btn": "🍪 Оновити Cookies",
        "admin_stats_btn": "📊 Статистика",
        "admin_feedback_btn": "💬 Модерація зворотного зв'язку",
        
        "stats_title": "📊 **Статистика бота:**\n\n• **Всього користувачів:** {total_users}\n• **Активних сьогодні (Київ):** {active_today}\n• **Забанених користувачів:** {banned_users}\n• **Адміністраторів:** {admins_count}\n• **Загалом завантажень:** {total_downloads}\n• **Відкритих тікетів:** {open_tickets}",

        "admin_enter_admin_id": "Надішліть Telegram ID користувача, якому хочете надати права адміна:",
        "admin_enter_channel_data": "Надішліть дані каналу:\n`@channel_id Назва_Каналу https://t.me/link`",
        "admin_enter_cookies": "🍪 Надішліть файл `cookies.txt` або його текст.",
        "cookies_updated": "✅ Cookies успішно збережено та оновлено!",
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
        "admin_info": "👑 **Адміністратор:** {name}\n🆔 **ID:** `{id}`\n📅 **Доданий:** {date}\n👤 **Ким доданий:** {added_by}",
        "admin_deleted": "✅ Адміністратора видалено!",
        "cant_delete_owner": "❌ Головного адміна видалити неможливо!",
        "admin_action_history_btn": "📜 Історія дій",
        "admin_history_title": "📜 **Історія дій адміна ({id}):**\n",
        "admin_history_empty": "Історія дій порожня.",
        
        "admin_search_user_prompt": "🔍 Надішліть Telegram ID користувача для пошуку:",
        "user_not_found": "❌ Користувача з таким ID не знайдено в базі.",
        "user_info_admin": "👤 **Профіль:** {name}\n🆔 **ID:** `{id}`\n🕒 **Останній онлайн (Київ):** {last_active}\n📥 **Завантажень:** {downloads}\n🚫 **Бан:** {banned}\n👑 **Адмін:** {is_admin}",
        "ban_btn": "🚫 Забанити",
        "unban_btn": "✅ Розбанити",
        "promote_admin_btn": "👑 Призначити адміном",
        "demote_admin_btn": "❌ Зняти з адмінів",
        "history_btn": "📥 Історія завантажень",
        "user_banned_success": "✅ Користувача забанено.",
        "user_unbanned_success": "✅ Користувача розбанено.",
        "user_promoted_admin": "✅ Користувача призначено адміністратором!",
        "user_demoted_admin": "✅ Права адміністратора відкликано.",
        "user_history_title": "📥 **Історія завантажень користувача ({id}):**\n",
        "user_history_empty": "Історія завантажень порожня.",
        
        "users_list_title": "👥 **Список учасників бота (Сторінка {page}/{total}):**",
        
        "caption_menu_title": "✍️ **Керування підписом до файлів:**\n\n• **Підпис бота:** `{bot_status}`\n• **Додаткова стрічка:** `{custom_text}`",
        "toggle_bot_caption_btn": "🤖 Підпис бота: {status}",
        "edit_custom_caption_btn": "✏️ Додаткова стрічка",
        "caption_prompt": "Надішліть текст, який буде додаватись після юзернейма бота (або надішліть `-`, щоб очистити):",
        "caption_saved": "✅ Підпис успішно оновлено!",
        
        "broadcast_constructor_title": "📢 **Конструктор розсилок**\n\nОберіть потрібний режим розсилки:",
        "btn_instant_broadcast": "🚀 Миттєва розсилка",
        "btn_scheduled_broadcast": "⏰ Розсилка за розкладом",
        "btn_interval_broadcast": "🔄 Розсилка кожні N запитів",
        "btn_manage_broadcasts": "📋 Активні розсилки",
        "broadcast_prompt": "📢 Надішліть повідомлення для розсилки (текст, фото, відео з кнопками чи без):",
        "broadcast_confirm_btn": "🚀 Підтвердити та запустити розсилку",
        "broadcast_started": "⏳ Розсилку розпочато...",
        "broadcast_finished": "✅ Розсилку завершено.\n\n• **Успішно:** {success}\n• **Помилок:** {failed}",
        "broadcast_scheduled_prompt": "⏰ Введіть дату та час розсилки по Київському часу у форматі `YYYY-MM-DD HH:MM` (наприклад, `{example}`):",
        "broadcast_scheduled_saved": "✅ Розсилку успішно заплановано на {time} (Київський час)!",
        "broadcast_interval_prompt": "🔄 Введіть інтервал запитів (наприклад `5` — розсилка надсилатиметься користувачу після кожного 5-го завантаження):",
        "broadcast_interval_saved": "✅ Автоматичну розсилку встановлено на кожен {count}-й запит!",
        "broadcast_list_title": "📋 **Список активних/запланованих розсилок:**",
        "broadcast_empty": "Немає активних або запланованих розсилок.",

        "feedback_menu_title": "💬 **Зворотній зв'язок**\n\nТут ви можете створити звернення до адміністрації або переглянути свої попередні тікети.",
        "btn_create_ticket": "📝 Написати звернення",
        "ticket_status_open": "🟡 В обробці",
        "ticket_status_answered": "🟢 Дано відповідь",
        "ticket_status_closed": "🔴 Закрито",
        "ticket_create_prompt": "📝 **Створення звернення**\n\nНадішліть ваші повідомлення (текст, фото або файли). Ви можете надіслати кілька повідомлень поспіль.\nКоли закінчите — натисніть кнопку **🚀 Відправити звернення** нижче.",
        "btn_send_ticket": "🚀 Відправити звернення",
        "ticket_sent_success": "✅ Ваше звернення №{id} успішно надіслано адміністраторам!",
        "ticket_empty_error": "⚠️ Ви ще не додали жодного повідомлення до звернення.",
        "ticket_view_title": "🎫 **Тікет №{id}**\n**Статус:** {status}\n**Створено (Київ):** {created_at}\n\n**Історія листування:**\n",
        "admin_tickets_title": "💬 **Модерація зворотного зв'язку**\n\nОберіть тікет для перегляду та відповіді:",
        "reply_ticket_btn": "💬 Відповісти",
        "close_ticket_btn": "🔒 Закрити тікет",
        "ticket_closed_msg": "✅ Тікет №{id} закритий.",
        "admin_ticket_reply_prompt": "💬 Надішліть відповідь для користувача (текст, фото або файл):",
        "admin_ticket_reply_sent": "✅ Відповідь успішно надіслано користувачу!",

        "action_cancelled": "✅ Дія скасована."
    },
    "en": {
        "btn_settings": "⚙️ Settings",
        "btn_profile": "👤 Profile",
        "btn_admin": "🔑 Admin Panel",
        "btn_feedback": "💬 Feedback",
        "start": "Hello! 👋\nSend a YouTube or YouTube Music link.\nVideo, track, or playlist supported.",
        "choose_format": "Choose format:",
        "audio_btn": "🎵 Audio",
        "video_btn": "🎬 Video",
        "download_audio": "🎵 Download audio",
        
        "terms_title": "📜 **Terms of Service**\n\nPlease read and confirm your agreement:\n1. This bot is intended for personal media downloading.\n2. You agree not to use the bot for illegal purposes.\n3. Administration is not responsible for user downloaded content.",
        "terms_accept_btn": "✅ Accept",
        "terms_decline_btn": "❌ Decline",
        "terms_declined_msg": "⚠️ You declined the terms. You cannot use the bot until accepted.",
        "terms_accepted_msg": "✅ Thank you! You have accepted the terms of service.",

        "settings_main": "⚙️ **Settings**\nSelect a section:",
        "lang_menu_btn": "🌐 Language",
        "settings_lang": "Select your preferred interface language:",
        "lang_set": "✅ Language successfully set to English 🇬🇧",
        
        "profile_text": "👤 **User Profile**\n\n**Name:** {name}\n**ID:** `{id}`\n**Status:** {status}\n**Last active (Kyiv):** {last_active}\n**Downloads:** {downloads}",
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
        "admin_users_btn": "🔍 Search User",
        "admin_all_users_btn": "👥 Users List",
        "admin_caption_btn": "✍️ Add Signature",
        "admin_broadcast_btn": "📢 Broadcast Constructor",
        "admin_cookies_btn": "🍪 Update Cookies",
        "admin_stats_btn": "📊 Statistics",
        "admin_feedback_btn": "💬 Feedback Moderation",
        
        "stats_title": "📊 **Bot Statistics:**\n\n• **Total Users:** {total_users}\n• **Active Today (Kyiv):** {active_today}\n• **Banned Users:** {banned_users}\n• **Administrators:** {admins_count}\n• **Total Downloads:** {total_downloads}\n• **Open Tickets:** {open_tickets}",

        "admin_enter_admin_id": "Send the Telegram ID to promote to admin:",
        "admin_enter_channel_data": "Send channel details:\n`@channel_id Name https://t.me/link`",
        "admin_enter_cookies": "🍪 Send the `cookies.txt` file or paste text.",
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
        "admin_info": "👑 **Admin:** {name}\n🆔 **ID:** `{id}`\n📅 **Added:** {date}\n👤 **Added by:** {added_by}",
        "admin_deleted": "✅ Admin removed!",
        "cant_delete_owner": "❌ Cannot remove the main owner!",
        "admin_action_history_btn": "📜 Action History",
        "admin_history_title": "📜 **Action History for Admin ({id}):**\n",
        "admin_history_empty": "Action history is empty.",
        
        "admin_search_user_prompt": "🔍 Send Telegram ID to search:",
        "user_not_found": "❌ User not found in DB.",
        "user_info_admin": "👤 **Profile:** {name}\n🆔 **ID:** `{id}`\n🕒 **Last active (Kyiv):** {last_active}\n📥 **Downloads:** {downloads}\n🚫 **Banned:** {banned}\n👑 **Admin:** {is_admin}",
        "ban_btn": "🚫 Ban",
        "unban_btn": "✅ Unban",
        "promote_admin_btn": "👑 Promote to Admin",
        "demote_admin_btn": "❌ Demote Admin",
        "history_btn": "📥 Download History",
        "user_banned_success": "✅ User banned.",
        "user_unbanned_success": "✅ User unbanned.",
        "user_promoted_admin": "✅ User promoted to admin!",
        "user_demoted_admin": "✅ Admin rights revoked.",
        "user_history_title": "📥 **Download History ({id}):**\n",
        "user_history_empty": "Download history is empty.",
        
        "users_list_title": "👥 **Bot Users List (Page {page}/{total}):**",
        
        "caption_menu_title": "✍️ **File Signature Settings:**\n\n• **Bot Signature:** `{bot_status}`\n• **Additional Line:** `{custom_text}`",
        "toggle_bot_caption_btn": "🤖 Bot Signature: {status}",
        "edit_custom_caption_btn": "✏️ Additional Line",
        "caption_prompt": "Send text for additional line after bot username (or send `-` to clear):",
        "caption_saved": "✅ Signature updated successfully!",
        
        "broadcast_constructor_title": "📢 **Broadcast Constructor**\n\nChoose broadcast mode:",
        "btn_instant_broadcast": "🚀 Instant Broadcast",
        "btn_scheduled_broadcast": "⏰ Scheduled Broadcast",
        "btn_interval_broadcast": "🔄 Broadcast every N requests",
        "btn_manage_broadcasts": "📋 Active Broadcasts",
        "broadcast_prompt": "📢 Send message for broadcast (text, photo, media, etc.):",
        "broadcast_confirm_btn": "🚀 Confirm and Start Broadcast",
        "broadcast_started": "⏳ Broadcast started...",
        "broadcast_finished": "✅ Broadcast completed.\n\n• **Successful:** {success}\n• **Failed:** {failed}",
        "broadcast_scheduled_prompt": "⏰ Enter date & time in Kyiv timezone (`YYYY-MM-DD HH:MM`, e.g. `{example}`):",
        "broadcast_scheduled_saved": "✅ Broadcast scheduled for {time} (Kyiv time)!",
        "broadcast_interval_prompt": "🔄 Enter request interval (e.g. `5` — broadcast will trigger every 5th download):",
        "broadcast_interval_saved": "✅ Automatic broadcast set for every {count}th download!",
        "broadcast_list_title": "📋 **Active/Scheduled Broadcasts:**",
        "broadcast_empty": "No active or scheduled broadcasts.",

        "feedback_menu_title": "💬 **Feedback**\n\nCreate a support ticket or view your ticket history.",
        "btn_create_ticket": "📝 Create Ticket",
        "ticket_status_open": "🟡 Pending",
        "ticket_status_answered": "🟢 Answered",
        "ticket_status_closed": "🔴 Closed",
        "ticket_create_prompt": "📝 **Create Support Ticket**\n\nSend your messages (text, photo, files). You can send multiple messages.\nWhen done, press **🚀 Submit Ticket** below.",
        "btn_send_ticket": "🚀 Submit Ticket",
        "ticket_sent_success": "✅ Ticket #{id} successfully sent to admins!",
        "ticket_empty_error": "⚠️ You haven't added any messages to the ticket yet.",
        "ticket_view_title": "🎫 **Ticket #{id}**\n**Status:** {status}\n**Created (Kyiv):** {created_at}\n\n**Messages:**\n",
        "admin_tickets_title": "💬 **Feedback Moderation**\n\nSelect a ticket to review and reply:",
        "reply_ticket_btn": "💬 Reply",
        "close_ticket_btn": "🔒 Close Ticket",
        "ticket_closed_msg": "✅ Ticket #{id} closed.",
        "admin_ticket_reply_prompt": "💬 Send reply to user (text, photo or file):",
        "admin_ticket_reply_sent": "✅ Reply sent successfully!",

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
    try: await app_bot.delete_my_commands()
    except: pass
    try: await app_bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    except: pass
    try: await app_bot.delete_my_commands(scope=BotCommandScopeDefault())
    except: pass

def get_main_keyboard(user_id: int, lang: str) -> ReplyKeyboardMarkup:
    keys = [
        [KeyboardButton(get_text(lang, "btn_settings")), KeyboardButton(get_text(lang, "btn_profile"))],
        [KeyboardButton(get_text(lang, "btn_feedback"))]
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
# YT-DLP CORE LOGIC (SAFE FORMAT SPECIFIERS)
# =========================================================

def youtube_options_base():
    options = {
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {"player_client": ["android_vr", "mweb", "web"]},
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
            if f.get("height") and f.get("height") == h:
                found = True
                sz = f.get("filesize") or f.get("filesize_approx") or 0
                if sz > video_bytes:
                    video_bytes = sz

        if found:
            total_bytes = video_bytes + audio_bytes
            mb = round(total_bytes / (1024 * 1024), 1) if total_bytes > 0 else 0
            available_qualities.append({"height": h, "size_mb": mb})

    if not available_qualities:
        available_qualities.append({"height": 720, "size_mb": 0})

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
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b",
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    })
    
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            mp4_file = str(Path(filename).with_suffix(".mp4"))
            if os.path.exists(mp4_file): return mp4_file, info
            video_files = [p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
            if video_files: return str(video_files[0]), info
    except Exception as e:
        logger.warning("Strict format download failed (%s). Retrying fallback format...", e)
        fallback_options = youtube_options_base()
        fallback_options.update({
            "format": "bestvideo+bestaudio/best",
            "outtmpl": output,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        })
        with YoutubeDL(fallback_options) as ydl:
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

async def show_terms_prompt(message_or_query, lang: str):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_text(lang, "terms_accept_btn"), callback_data="terms_accept"),
            InlineKeyboardButton(get_text(lang, "terms_decline_btn"), callback_data="terms_decline")
        ]
    ])
    text = get_text(lang, "terms_title")
    if hasattr(message_or_query, "edit_text"):
        await message_or_query.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_query.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    first_name = user.first_name or ""
    register_or_update_user(user.id, username, first_name)
    
    lang = get_user_lang(user.id)
    if not check_terms_accepted(user.id):
        await show_terms_prompt(update.message, lang)
        return

    await update.message.reply_text(
        get_text(lang, "start"),
        reply_markup=get_main_keyboard(user.id, lang)
    )

async def master_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    register_or_update_user(user_id, user.username or "", user.first_name or "")
    lang = get_user_lang(user_id)

    if is_user_banned(user_id):
        await update.message.reply_text(get_text(lang, "banned_text"))
        return

    if not check_terms_accepted(user_id):
        await show_terms_prompt(update.message, lang)
        return

    admin_state = context.user_data.get("admin_state")
    text = update.message.text.strip() if update.message.text else ""

    # Feedback / Ticket creation mode for user
    if context.user_data.get("ticket_mode"):
        draft = context.user_data.get("ticket_draft", [])
        if update.message.text:
            draft.append({"type": "text", "content": update.message.text})
        elif update.message.photo:
            draft.append({"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or ""})
        elif update.message.document:
            draft.append({"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or ""})
        context.user_data["ticket_draft"] = draft

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(get_text(lang, "btn_send_ticket"), callback_data="ticket_submit")],
            [InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data="ticket_cancel")]
        ])
        await update.message.reply_text(f"📥 Додано до звернення (повідомлень: {len(draft)}).\nКоли завершите, натисніть **{get_text(lang, 'btn_send_ticket')}**.", reply_markup=keyboard, parse_mode="Markdown")
        return

    if admin_state:
        await handle_admin_inputs(update, context, text, admin_state, lang)
        return

    if text in [TEXTS["ua"]["btn_settings"], TEXTS["en"]["btn_settings"]]:
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "lang_menu_btn"), callback_data="settings_lang")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await update.message.reply_text(get_text(lang, "settings_main"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if text in [TEXTS["ua"]["btn_profile"], TEXTS["en"]["btn_profile"]]:
        info = get_user_info(user_id)
        status = get_text(lang, "status_admin") if is_admin(user_id) else get_text(lang, "status_user")
        last_act = info[7] if (len(info) > 7 and info[7]) else (info[6] if len(info) > 6 else "-")
        
        profile_msg = get_text(lang, "profile_text").format(
            name=info[3] or info[2] or f"ID: {user_id}",
            id=user_id,
            status=status,
            last_active=last_act,
            downloads=info[4] if len(info) > 4 else 0
        )
        await update.message.reply_text(profile_msg, parse_mode="Markdown")
        return

    if text in [TEXTS["ua"]["btn_feedback"], TEXTS["en"]["btn_feedback"]]:
        tickets = get_user_tickets(user_id)
        keyboard = []
        for t in tickets:
            st = get_text(lang, f"ticket_status_{t[1]}")
            keyboard.append([InlineKeyboardButton(f"🎫 №{t[0]} ({st}) — {t[2]}", callback_data=f"tkt_view:{t[0]}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "btn_create_ticket"), callback_data="ticket_start")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")])
        await update.message.reply_text(get_text(lang, "feedback_menu_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if text in [TEXTS["ua"]["btn_admin"], TEXTS["en"]["btn_admin"]]:
        if not is_admin(user_id): return
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users"), InlineKeyboardButton(get_text(lang, "admin_all_users_btn"), callback_data="users_page:1")],
            [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels"), InlineKeyboardButton(get_text(lang, "admin_feedback_btn"), callback_data="admin_tickets_list")],
            [InlineKeyboardButton(get_text(lang, "admin_caption_btn"), callback_data="admin_caption_menu"), InlineKeyboardButton(get_text(lang, "admin_broadcast_btn"), callback_data="admin_broadcast_menu")],
            [InlineKeyboardButton(get_text(lang, "admin_stats_btn"), callback_data="admin_stats"), InlineKeyboardButton(get_text(lang, "admin_cookies_btn"), callback_data="admin_cookies")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await update.message.reply_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if text:
        unsubscribed = await check_user_subscriptions(context.bot, user_id)
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
    
    user = update.effective_user
    user_id = user.id
    register_or_update_user(user_id, user.username or "", user.first_name or "")
    
    data = query.data
    lang = get_user_lang(user_id)

    if data == "terms_accept":
        set_terms_accepted(user_id, True)
        await query.edit_message_text(get_text(lang, "terms_accepted_msg"))
        await query.message.reply_text(get_text(lang, "start"), reply_markup=get_main_keyboard(user_id, lang))
        return

    if data == "terms_decline":
        set_terms_accepted(user_id, False)
        await query.edit_message_text(get_text(lang, "terms_declined_msg"))
        return

    if is_user_banned(user_id):
        await query.edit_message_text(get_text(lang, "banned_text"))
        return

    if data == "close_menu":
        context.user_data["admin_state"] = None
        context.user_data["ticket_mode"] = False
        await query.message.delete()
        return
        
    if data == "cancel_admin_action":
        context.user_data["admin_state"] = None
        await query.edit_message_text(get_text(lang, "action_cancelled"))
        return
        
    if data == "cancel_to_admin_menu":
        context.user_data["admin_state"] = None
        data = "admin_menu"

    if data == "noop":
        return

    # --- SETTINGS & LANG ---
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

    # --- TICKET SYSTEM (USER) ---
    if data == "ticket_start":
        context.user_data["ticket_mode"] = True
        context.user_data["ticket_draft"] = []
        await query.edit_message_text(get_text(lang, "ticket_create_prompt"), reply_markup=get_cancel_inline(lang, "ticket_cancel"), parse_mode="Markdown")
        return

    if data == "ticket_cancel":
        context.user_data["ticket_mode"] = False
        context.user_data["ticket_draft"] = []
        await query.edit_message_text(get_text(lang, "action_cancelled"))
        return

    if data == "ticket_submit":
        draft = context.user_data.get("ticket_draft", [])
        if not draft:
            await query.answer(get_text(lang, "ticket_empty_error"), show_alert=True)
            return
        
        ticket_id = create_ticket(user_id)
        for item in draft:
            add_ticket_message(ticket_id, "user", item["type"], item.get("content", ""), item.get("file_id", ""))
            
        context.user_data["ticket_mode"] = False
        context.user_data["ticket_draft"] = []
        await query.edit_message_text(get_text(lang, "ticket_sent_success").format(id=ticket_id))
        return

    if data.startswith("tkt_view:"):
        t_id = int(data.split(":")[1])
        t_info = get_ticket_info(t_id)
        if not t_info: return
        msgs = get_ticket_messages(t_id)
        st = get_text(lang, f"ticket_status_{t_info[2]}")
        text = get_text(lang, "ticket_view_title").format(id=t_id, status=st, created_at=t_info[3])
        
        for m in msgs:
            sender_lbl = get_text(lang, "status_admin") if m[1] == "admin" else get_text(lang, "status_user")
            text += f"▪️ **{sender_lbl}** ({m[5]}):\n{m[3] or '[Файл/Медіа]'}\n\n"
            
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="close_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # --- ADMIN CALLBACKS ---
    if data == "admin_menu" and is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users"), InlineKeyboardButton(get_text(lang, "admin_all_users_btn"), callback_data="users_page:1")],
            [InlineKeyboardButton(get_text(lang, "admin_list_channels_btn"), callback_data="admin_list_channels"), InlineKeyboardButton(get_text(lang, "admin_feedback_btn"), callback_data="admin_tickets_list")],
            [InlineKeyboardButton(get_text(lang, "admin_caption_btn"), callback_data="admin_caption_menu"), InlineKeyboardButton(get_text(lang, "admin_broadcast_btn"), callback_data="admin_broadcast_menu")],
            [InlineKeyboardButton(get_text(lang, "admin_stats_btn"), callback_data="admin_stats"), InlineKeyboardButton(get_text(lang, "admin_cookies_btn"), callback_data="admin_cookies")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await query.edit_message_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "admin_stats" and is_admin(user_id):
        tot_users = execute_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
        today_str = get_kyiv_now_str().split()[0]
        act_today = execute_query("SELECT COUNT(*) FROM users WHERE last_active LIKE ?", (f"{today_str}%",), fetchone=True)[0]
        banned_users = execute_query("SELECT COUNT(*) FROM users WHERE is_banned = TRUE", fetchone=True)[0]
        adm_count = execute_query("SELECT COUNT(*) FROM admins", fetchone=True)[0]
        tot_dl = execute_query("SELECT SUM(downloads) FROM users", fetchone=True)[0] or 0
        open_tkts = execute_query("SELECT COUNT(*) FROM tickets WHERE status = 'open'", fetchone=True)[0]

        stats_msg = get_text(lang, "stats_title").format(
            total_users=tot_users,
            active_today=act_today,
            banned_users=banned_users,
            admins_count=adm_count,
            total_downloads=tot_dl,
            open_tickets=open_tkts
        )
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")]]
        await query.edit_message_text(stats_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "admin_list_admins" and is_admin(user_id):
        admins = get_all_admins_info()
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin")]
        ]
        for adm in admins:
            display_name = get_user_display_name(adm[0])
            keyboard.append([InlineKeyboardButton(f"👑 {display_name}", callback_data=f"adm_view:{adm[0]}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "admins_list_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("adm_view:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        info = get_admin_info(adm_id)
        if not info: return
        added_by_name = get_user_display_name(info[1])
        adm_name = get_user_display_name(info[0])
        text = get_text(lang, "admin_info").format(name=adm_name, id=info[0], date=info[2] or "-", added_by=added_by_name)
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_action_history_btn"), callback_data=f"adm_hist:{adm_id}")]
        ]
        if adm_id != INITIAL_ADMIN_ID:
            keyboard.append([InlineKeyboardButton(get_text(lang, "delete_btn"), callback_data=f"adm_del:{adm_id}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_list_admins")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("adm_hist:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        logs = get_admin_logs(adm_id)
        text = get_text(lang, "admin_history_title").format(id=adm_id)
        if not logs:
            text += get_text(lang, "admin_history_empty")
        else:
            for l in logs:
                text += f"• `{l[1]}` — {l[0]}\n"
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"adm_view:{adm_id}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if data.startswith("adm_del:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        if adm_id == INITIAL_ADMIN_ID:
            await query.answer(get_text(lang, "cant_delete_owner"), show_alert=True)
            return
        remove_admin(adm_id, removed_by=user_id)
        await query.answer(get_text(lang, "admin_deleted"), show_alert=True)
        data = "admin_list_admins"

    if data.startswith("usr_make_admin:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        add_admin(target_id, added_by=user_id)
        await query.answer(get_text(lang, "user_promoted_admin"), show_alert=True)
        data = f"usr_view:{target_id}"

    if data.startswith("usr_rm_admin:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        remove_admin(target_id, removed_by=user_id)
        await query.answer(get_text(lang, "user_demoted_admin"), show_alert=True)
        data = f"usr_view:{target_id}"

    if data.startswith("users_page:") and is_admin(user_id):
        page = int(data.split(":")[1])
        rows, total_pages = get_users_page(page, 10)
        
        keyboard = []
        for u in rows:
            uid, _, uname, fname, _, banned, _ = u
            display_name = fname or uname or f"ID: {uid}"
            if banned: display_name = f"🚫 {display_name}"
            keyboard.append([InlineKeyboardButton(display_name, callback_data=f"usr_view:{uid}")])
            
        nav_buttons = []
        if page > 1: nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"users_page:{page-1}"))
        nav_buttons.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        if page < total_pages: nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"users_page:{page+1}"))
        if nav_buttons: keyboard.append(nav_buttons)
            
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        text = get_text(lang, "users_list_title").format(page=page, total=total_pages)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("usr_view:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        info = get_user_info(target_id)
        if not info:
            await query.answer(get_text(lang, "user_not_found"), show_alert=True)
            return
        
        banned = info[5]
        last_act = info[7] if (len(info) > 7 and info[7]) else (info[6] if len(info) > 6 else "-")
        target_is_admin = is_admin(target_id)
        
        text = get_text(lang, "user_info_admin").format(
            name=info[3] or info[2] or f"ID: {target_id}", 
            id=info[0], 
            last_active=last_act, 
            downloads=info[4] if len(info)>4 else 0,
            banned="🔴 Так" if banned else "🟢 Ні",
            is_admin="👑 Так" if target_is_admin else "👤 Ні"
        )
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "history_btn"), callback_data=f"usr_hist:{target_id}")],
            [
                InlineKeyboardButton(get_text(lang, "unban_btn") if banned else get_text(lang, "ban_btn"), callback_data=f"usr_unban:{target_id}" if banned else f"usr_ban:{target_id}"),
                InlineKeyboardButton(get_text(lang, "demote_admin_btn") if target_is_admin else get_text(lang, "promote_admin_btn"), callback_data=f"usr_rm_admin:{target_id}" if target_is_admin else f"usr_make_admin:{target_id}")
            ],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="users_page:1")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("usr_ban:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        set_user_ban(target_id, True)
        log_admin_action(user_id, f"Забанено користувача ID: {target_id}")
        await query.answer(get_text(lang, "user_banned_success"), show_alert=True)
        data = f"usr_view:{target_id}"
        
    if data.startswith("usr_unban:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        set_user_ban(target_id, False)
        log_admin_action(user_id, f"Розбанено користувача ID: {target_id}")
        await query.answer(get_text(lang, "user_unbanned_success"), show_alert=True)
        data = f"usr_view:{target_id}"
        
    if data.startswith("usr_hist:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        hist = get_user_history(target_id, 20)
        text = get_text(lang, "user_history_title").format(id=target_id)
        if not hist:
            text += get_text(lang, "user_history_empty")
        else:
            for i, h in enumerate(hist):
                text += f"{i+1}. `{h[1]}`\n🔗 {h[0]}\n"
        
        keyboard = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"usr_view:{target_id}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown", disable_web_page_preview=True)
        return

    # --- TICKET MODERATION (ADMIN) ---
    if data == "admin_tickets_list" and is_admin(user_id):
        tkts = get_all_tickets()
        keyboard = []
        for t in tkts:
            st = get_text(lang, f"ticket_status_{t[2]}")
            uname = get_user_display_name(t[1])
            keyboard.append([InlineKeyboardButton(f"🎫 №{t[0]} ({st}) — {uname}", callback_data=f"adm_tkt_view:{t[0]}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "admin_tickets_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("adm_tkt_view:") and is_admin(user_id):
        t_id = int(data.split(":")[1])
        t_info = get_ticket_info(t_id)
        if not t_info: return
        msgs = get_ticket_messages(t_id)
        st = get_text(lang, f"ticket_status_{t_info[2]}")
        uname = get_user_display_name(t_info[1])
        
        text = f"🎫 **Тікет №{t_id}**\n**Користувач:** {uname} (`{t_info[1]}`)\n**Статус:** {st}\n**Створено (Київ):** {t_info[3]}\n\n"
        for m in msgs:
            sender_lbl = "👑 Адмін" if m[1] == "admin" else "👤 Користувач"
            text += f"▪️ **{sender_lbl}** ({m[5]}):\n{m[3] or '[Файл/Медіа]'}\n\n"
            
        keyboard = [
            [
                InlineKeyboardButton(get_text(lang, "reply_ticket_btn"), callback_data=f"adm_tkt_reply:{t_id}"),
                InlineKeyboardButton(get_text(lang, "close_ticket_btn"), callback_data=f"adm_tkt_close:{t_id}")
            ],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_tickets_list")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("adm_tkt_reply:") and is_admin(user_id):
        t_id = int(data.split(":")[1])
        context.user_data["admin_state"] = f"await_ticket_reply:{t_id}"
        await query.edit_message_text(get_text(lang, "admin_ticket_reply_prompt"), reply_markup=get_cancel_inline(lang, f"adm_tkt_view:{t_id}"))
        return

    if data.startswith("adm_tkt_close:") and is_admin(user_id):
        t_id = int(data.split(":")[1])
        update_ticket_status(t_id, "closed")
        await query.answer(get_text(lang, "ticket_closed_msg").format(id=t_id), show_alert=True)
        data = f"adm_tkt_view:{t_id}"

    # --- BROADCAST CONSTRUCTOR (ADMIN) ---
    if data == "admin_broadcast_menu" and is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "btn_instant_broadcast"), callback_data="bc_instant")],
            [InlineKeyboardButton(get_text(lang, "btn_scheduled_broadcast"), callback_data="bc_scheduled")],
            [InlineKeyboardButton(get_text(lang, "btn_interval_broadcast"), callback_data="bc_interval")],
            [InlineKeyboardButton(get_text(lang, "btn_manage_broadcasts"), callback_data="bc_manage")],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")]
        ]
        await query.edit_message_text(get_text(lang, "broadcast_constructor_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "bc_instant" and is_admin(user_id):
        context.user_data["admin_state"] = "await_broadcast_message"
        context.user_data["broadcast_type"] = "instant"
        await query.edit_message_text(get_text(lang, "broadcast_prompt"), reply_markup=get_cancel_inline(lang, "admin_broadcast_menu"))
        return

    if data == "bc_scheduled" and is_admin(user_id):
        context.user_data["admin_state"] = "await_broadcast_message"
        context.user_data["broadcast_type"] = "scheduled"
        await query.edit_message_text(get_text(lang, "broadcast_prompt"), reply_markup=get_cancel_inline(lang, "admin_broadcast_menu"))
        return

    if data == "bc_interval" and is_admin(user_id):
        context.user_data["admin_state"] = "await_broadcast_message"
        context.user_data["broadcast_type"] = "interval"
        await query.edit_message_text(get_text(lang, "broadcast_prompt"), reply_markup=get_cancel_inline(lang, "admin_broadcast_menu"))
        return

    if data == "bc_manage" and is_admin(user_id):
        bcs = execute_query("SELECT id, type, scheduled_at, trigger_count, status FROM broadcasts ORDER BY id DESC", fetchall=True)
        keyboard = []
        for b in bcs:
            info_str = f"#{b[0]} {b[1]} | {b[2] or f'Кожні {b[3]} завантажень'} | {b[4]}"
            keyboard.append([InlineKeyboardButton(info_str, callback_data="noop"), InlineKeyboardButton("🗑", callback_data=f"bc_del:{b[0]}")])
        keyboard.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_broadcast_menu")])
        text = get_text(lang, "broadcast_list_title") if bcs else get_text(lang, "broadcast_empty")
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data.startswith("bc_del:") and is_admin(user_id):
        bc_id = int(data.split(":")[1])
        execute_query("DELETE FROM broadcasts WHERE id = ?", (bc_id,), commit=True)
        await query.answer("✅ Розсилку видалено!", show_alert=True)
        data = "bc_manage"

    if data == "broadcast_confirm_send" and is_admin(user_id):
        source = context.user_data.get("broadcast_source")
        if not source: return
        chat_id, msg_id = source
        context.user_data["admin_state"] = None
        await query.edit_message_text(get_text(lang, "broadcast_started"))
        
        users = execute_query("SELECT user_id FROM users WHERE is_banned = FALSE", fetchall=True)
        success = 0
        failed = 0
        for u in users:
            uid = u[0]
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id)
                success += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1
                
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_text(lang, "broadcast_finished").format(success=success, failed=failed)
        )
        return

    # --- OTHER ADMIN CONTROLS ---
    if data == "admin_caption_menu" and is_admin(user_id):
        bot_en = execute_query("SELECT value FROM settings WHERE key = 'caption_bot_enabled'", fetchone=True)
        custom_txt = execute_query("SELECT value FROM settings WHERE key = 'caption_custom_text'", fetchone=True)
        is_bot_on = bot_en and bot_en[0] == "true"
        custom_val = custom_txt[0] if custom_txt and custom_txt[0] else "Не задано"
        
        status_str = "Ввімкнено 🟢" if is_bot_on else "Вимкнено 🔴"
        text = get_text(lang, "caption_menu_title").format(bot_status=status_str, custom_text=custom_val)
        
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "toggle_bot_caption_btn").format(status=status_str), callback_data="caption_toggle_bot")],
            [InlineKeyboardButton(get_text(lang, "edit_custom_caption_btn"), callback_data="caption_set_custom")],
            [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if data == "caption_toggle_bot" and is_admin(user_id):
        bot_en = execute_query("SELECT value FROM settings WHERE key = 'caption_bot_enabled'", fetchone=True)
        is_bot_on = bot_en and bot_en[0] == "true"
        new_state = "false" if is_bot_on else "true"
        execute_query("UPDATE settings SET value = ? WHERE key = 'caption_bot_enabled'", (new_state,), commit=True)
        data = "admin_caption_menu"

    if data == "caption_set_custom" and is_admin(user_id):
        context.user_data["admin_state"] = "await_caption_custom"
        await query.edit_message_text(get_text(lang, "caption_prompt"), reply_markup=get_cancel_inline(lang, "admin_caption_menu"))
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
        data = "admin_list_channels"

    if data.startswith("sp_edit:") and is_admin(user_id):
        ch_id = data.split(":", 1)[1]
        context.user_data["admin_state"] = f"await_edit_channel:{ch_id}"
        await query.edit_message_text(get_text(lang, "edit_sponsor_prompt"), reply_markup=get_cancel_inline(lang, f"sp_view:{ch_id}"), parse_mode="Markdown")
        return

    # --- INLINE DOWNLOAD CALLBACKS (MEDIA EDITED DIRECTLY IN CHAT) ---
    inline_msg_id = query.inline_message_id

    if data.startswith("i_audio:"):
        url_id = int(data.split(":")[1])
        url = get_inline_url(url_id)
        if not url:
            await query.answer(get_text(lang, "link_lost"), show_alert=True)
            return

        if inline_msg_id:
            await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=get_text(lang, "downloading_audio"))
        else:
            status = await query.edit_message_text(get_text(lang, "downloading_audio"))

        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            file_size = os.path.getsize(filepath)
            increment_downloads(user_id, url, context.bot)
            caption = get_file_caption(context.bot.username)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    sent_msg = await context.bot.send_audio(
                        chat_id=user_id,
                        audio=f,
                        title=info.get("title", "audio")[:64],
                        performer=info.get("artist") or info.get("uploader"),
                        duration=int(info.get("duration", 0)) or None,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                if inline_msg_id and sent_msg.audio:
                    await context.bot.edit_message_media(
                        inline_message_id=inline_msg_id,
                        media=InputMediaAudio(media=sent_msg.audio.file_id, caption=caption, parse_mode="Markdown")
                    )
                elif 'status' in locals():
                    await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                full_text = get_text(lang, "file_too_large_audio").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}")
                if caption: full_text += f"\n\n{caption}"
                
                if inline_msg_id:
                    await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=full_text, parse_mode="Markdown")
                else:
                    await status.edit_text(full_text, parse_mode="Markdown")
        except Exception as error:
            logger.exception("Inline Download failed")
            err_msg = f"❌ {human_youtube_error(error)}"
            if inline_msg_id: await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=err_msg)
            elif 'status' in locals(): await status.edit_text(err_msg)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    if data.startswith("i_video:"):
        url_id = int(data.split(":")[1])
        url = get_inline_url(url_id)
        if not url:
            await query.answer(get_text(lang, "link_lost"), show_alert=True)
            return

        if inline_msg_id:
            await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=get_text(lang, "fetching_qualities"))
        else:
            status = await query.edit_message_text(get_text(lang, "fetching_qualities"))

        try:
            qualities, title = await asyncio.to_thread(get_video_formats_info, url)
            if not qualities:
                msg = get_text(lang, "no_qualities")
                if inline_msg_id: await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=msg)
                else: await status.edit_text(msg)
                return

            keyboard = [[InlineKeyboardButton(f"🎬 {q['height']}p (~{q['size_mb']} МБ)", callback_data=f"i_vdl:{url_id}:{q['height']}")] for q in qualities]
            msg = get_text(lang, "choose_quality").format(title=title[:60])
            if inline_msg_id:
                await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            else:
                await status.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as error:
            logger.exception("Format fetch failed")
            err_msg = f"❌ {human_youtube_error(error)}"
            if inline_msg_id: await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=err_msg)
            elif 'status' in locals(): await status.edit_text(err_msg)
        return

    if data.startswith("i_vdl:"):
        parts = data.split(":")
        url_id = int(parts[1])
        height = int(parts[2])
        url = get_inline_url(url_id)
        if not url:
            await query.answer(get_text(lang, "link_lost"), show_alert=True)
            return

        msg_dl = get_text(lang, "downloading_video").format(height=height)
        if inline_msg_id:
            await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=msg_dl)
        else:
            status = await query.edit_message_text(msg_dl)

        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_video_quality, url, workdir, height)
            file_size = os.path.getsize(filepath)
            increment_downloads(user_id, url, context.bot)
            caption = get_file_caption(context.bot.username)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    sent_msg = await context.bot.send_video(
                        chat_id=user_id,
                        video=f,
                        supports_streaming=True,
                        duration=int(info.get("duration", 0)) or None,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                if inline_msg_id and sent_msg.video:
                    await context.bot.edit_message_media(
                        inline_message_id=inline_msg_id,
                        media=InputMediaVideo(media=sent_msg.video.file_id, caption=caption, parse_mode="Markdown")
                    )
                elif 'status' in locals():
                    await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                full_text = get_text(lang, "file_too_large_video").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}")
                if caption: full_text += f"\n\n{caption}"

                if inline_msg_id:
                    await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=full_text, parse_mode="Markdown")
                else:
                    await status.edit_text(full_text, parse_mode="Markdown")
        except Exception as error:
            logger.exception("Inline Download failed")
            err_msg = f"❌ {human_youtube_error(error)}"
            if inline_msg_id: await context.bot.edit_message_text(inline_message_id=inline_msg_id, text=err_msg)
            elif 'status' in locals(): await status.edit_text(err_msg)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    # --- STANDARD DOWNLOAD CALLBACKS ---
    if data == "back_to_format":
        url = context.user_data.get("url")
        if url: await send_format_selection(query.message, url, lang, is_edit=True)
        else: await query.edit_message_text(get_text(lang, "link_lost"))
        return

    url = context.user_data.get("url")
    if not url: return

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
            increment_downloads(user_id, url, context.bot)
            caption = get_file_caption(context.bot.username)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_video(chat_id=query.message.chat_id, video=f, supports_streaming=True, duration=int(info.get("duration", 0)) or None, caption=caption, parse_mode="Markdown")
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                full_text = get_text(lang, "file_too_large_video").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}")
                if caption: full_text += f"\n\n{caption}"
                await status.edit_text(full_text, parse_mode="Markdown")
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
            increment_downloads(user_id, url, context.bot)
            caption = get_file_caption(context.bot.username)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, title=info.get("title", "audio")[:64], performer=info.get("artist") or info.get("uploader"), duration=int(info.get("duration", 0)) or None, caption=caption, parse_mode="Markdown")
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                full_text = get_text(lang, "file_too_large_audio").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}")
                if caption: full_text += f"\n\n{caption}"
                await status.edit_text(full_text, parse_mode="Markdown")
        except Exception as error:
            logger.exception("Download failed")
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    
    query = update.inline_query.query.strip()
    lang = get_user_lang(user.id)

    if not query or not is_youtube_url(query):
        results = [
            InlineQueryResultArticle(
                id="help",
                title="YouTube Downloader",
                description="Надішліть посилання на YouTube або YouTube Music",
                input_message_content=InputTextMessageContent(
                    message_text="👋 Надішліть посилання на YouTube або YouTube Music у чат з ботом або скористайтесь inline режимом: `@botusername <посилання>`"
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=1)
        return

    url_id = save_inline_url(query)
    
    results = [
        InlineQueryResultArticle(
            id=str(url_id),
            title="📥 Завантажити медіа з YouTube",
            description=query,
            input_message_content=InputTextMessageContent(
                message_text=f"🔗 **Посилання:** {query}\n\nОберіть формат завантаження:",
                parse_mode="Markdown"
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎵 Аудіо", callback_data=f"i_audio:{url_id}"),
                    InlineKeyboardButton("🎬 Відео", callback_data=f"i_video:{url_id}")
                ]
            ])
        )
    ]
    await update.inline_query.answer(results, cache_time=1)


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
        log_admin_action(user_id, "Оновлено файли cookies.txt")
        await update.message.reply_text(get_text(lang, "cookies_updated"))


async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, state: str, lang: str):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    if state.startswith("await_ticket_reply:"):
        t_id = int(state.split(":")[1])
        t_info = get_ticket_info(t_id)
        if not t_info: return
        
        reply_text = text or (update.message.caption if update.message.caption else "")
        file_id = ""
        msg_type = "text"
        if update.message.photo:
            msg_type = "photo"
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            msg_type = "document"
            file_id = update.message.document.file_id

        add_ticket_message(t_id, "admin", msg_type, reply_text, file_id)
        update_ticket_status(t_id, "answered")
        log_admin_action(user_id, f"Відповідно на тікет №{t_id}")
        context.user_data["admin_state"] = None
        
        # Сповіщення користувача
        try:
            notify_txt = f"🔔 **Надійшла відповідь на ваш тікет №{t_id}:**\n\n{reply_text}"
            if msg_type == "photo":
                await context.bot.send_photo(chat_id=t_info[1], photo=file_id, caption=notify_txt, parse_mode="Markdown")
            elif msg_type == "document":
                await context.bot.send_document(chat_id=t_info[1], document=file_id, caption=notify_txt, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=t_info[1], text=notify_txt, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Could not notify user for ticket reply: %s", e)

        await update.message.reply_text(get_text(lang, "admin_ticket_reply_sent"))
        return

    if state == "await_cookies":
        save_db_cookies(text)
        context.user_data["admin_state"] = None
        log_admin_action(user_id, "Оновлено cookies (текст)")
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
            last_act = info[7] if (len(info) > 7 and info[7]) else (info[6] if len(info) > 6 else "-")
            target_is_admin = is_admin(target_id)
            
            profile_text = get_text(lang, "user_info_admin").format(
                name=info[3] or info[2] or f"ID: {target_id}", 
                id=info[0], 
                last_active=last_act, 
                downloads=info[4] if len(info)>4 else 0,
                banned="🔴 Так" if banned else "🟢 Ні",
                is_admin="👑 Так" if target_is_admin else "👤 Ні"
            )
            keyboard = [
                [InlineKeyboardButton(get_text(lang, "history_btn"), callback_data=f"usr_hist:{target_id}")],
                [
                    InlineKeyboardButton(get_text(lang, "unban_btn") if banned else get_text(lang, "ban_btn"), callback_data=f"usr_unban:{target_id}" if banned else f"usr_ban:{target_id}"),
                    InlineKeyboardButton(get_text(lang, "demote_admin_btn") if target_is_admin else get_text(lang, "promote_admin_btn"), callback_data=f"usr_rm_admin:{target_id}" if target_is_admin else f"usr_make_admin:{target_id}")
                ],
                [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
            ]
            await update.message.reply_text(profile_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_id"))

    elif state == "await_channel_data":
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            add_sponsored_channel(parts[0], parts[1], parts[2])
            context.user_data["admin_state"] = None
            log_admin_action(user_id, f"Додано спонсора: {parts[1]}")
            await update.message.reply_text(get_text(lang, "admin_channel_added").format(title=parts[1]), parse_mode="Markdown")
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_channel_format"))

    elif state.startswith("await_edit_channel:"):
        ch_id = state.split(":", 1)[1]
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            update_sponsored_channel(ch_id, parts[0], parts[1])
            context.user_data["admin_state"] = None
            log_admin_action(user_id, f"Оновлено спонсора ID: {ch_id}")
            await update.message.reply_text(get_text(lang, "sponsor_updated"))
        else:
            await update.message.reply_text("❌ Формат: `Назва Посилання`")

    elif state == "await_caption_custom":
        val = "" if text == "-" else text
        execute_query("UPDATE settings SET value = ? WHERE key = 'caption_custom_text'", (val,), commit=True)
        context.user_data["admin_state"] = None
        log_admin_action(user_id, "Змінено додатковий підпис")
        await update.message.reply_text(get_text(lang, "caption_saved"))

    elif state == "await_broadcast_message":
        context.user_data["broadcast_source"] = (update.effective_chat.id, update.message.message_id)
        bc_type = context.user_data.get("broadcast_type", "instant")
        
        if bc_type == "instant":
            context.user_data["admin_state"] = "await_broadcast_confirm"
            keyboard = [[InlineKeyboardButton(get_text(lang, "broadcast_confirm_btn"), callback_data="broadcast_confirm_send")],
                        [InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data="cancel_to_admin_menu")]]
            await update.message.reply_text("📋 Попередній перегляд повідомлення для розсилки вище ⬆️\nПідтвердіть відправку:", reply_markup=InlineKeyboardMarkup(keyboard))
        
        elif bc_type == "scheduled":
            context.user_data["admin_state"] = "await_broadcast_time"
            example_time = get_kyiv_now_str()
            await update.message.reply_text(get_text(lang, "broadcast_scheduled_prompt").format(example=example_time), parse_mode="Markdown")

        elif bc_type == "interval":
            context.user_data["admin_state"] = "await_broadcast_interval"
            await update.message.reply_text(get_text(lang, "broadcast_interval_prompt"), parse_mode="Markdown")

    elif state == "await_broadcast_time":
        source = context.user_data.get("broadcast_source")
        if not source: return
        now_str = get_kyiv_now_str()
        execute_query("""
            INSERT INTO broadcasts (type, scheduled_at, chat_id, message_id, status, created_at)
            VALUES ('scheduled', ?, ?, ?, 'pending', ?)
        """, (text, source[0], source[1], now_str), commit=True)
        context.user_data["admin_state"] = None
        log_admin_action(user_id, f"Заплановано розсилку на {text}")
        await update.message.reply_text(get_text(lang, "broadcast_scheduled_saved").format(time=text))

    elif state == "await_broadcast_interval":
        if text.isdigit():
            count = int(text)
            source = context.user_data.get("broadcast_source")
            if not source: return
            now_str = get_kyiv_now_str()
            execute_query("""
                INSERT INTO broadcasts (type, trigger_count, chat_id, message_id, status, created_at)
                VALUES ('trigger', ?, ?, ?, 'active', ?)
            """, (count, source[0], source[1], now_str), commit=True)
            context.user_data["admin_state"] = None
            log_admin_action(user_id, f"Встановлено тригерну розсилку кожні {count} запитів")
            await update.message.reply_text(get_text(lang, "broadcast_interval_saved").format(count=count))
        else:
            await update.message.reply_text(get_text(lang, "admin_invalid_id"))


# =========================================================
# BACKGROUND WORKERS
# =========================================================

async def scheduled_broadcast_worker():
    await asyncio.sleep(15)
    while True:
        try:
            now_str = get_kyiv_now_str()
            pending = execute_query("SELECT id, chat_id, message_id FROM broadcasts WHERE type = 'scheduled' AND status = 'pending' AND scheduled_at <= ?", (now_str,), fetchall=True)
            for b in pending:
                bid, chat_id, msg_id = b
                execute_query("UPDATE broadcasts SET status = 'in_progress' WHERE id = ?", (bid,), commit=True)
                users = execute_query("SELECT user_id FROM users WHERE is_banned = FALSE", fetchall=True)
                for u in users:
                    uid = u[0]
                    try:
                        await telegram_app.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id)
                        await asyncio.sleep(0.04)
                    except Exception:
                        pass
                execute_query("UPDATE broadcasts SET status = 'completed' WHERE id = ?", (bid,), commit=True)
        except Exception as e:
            logger.error("Error in scheduled broadcast worker: %s", e)
        await asyncio.sleep(30)


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


# =========================================================
# APPLICATION SETUP & LIFESPAN
# =========================================================

telegram_app = Application.builder().token(TOKEN).updater(None).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(InlineQueryHandler(inline_query_handler))
telegram_app.add_handler(MessageHandler(filters.Document.ALL, handle_admin_doc))
telegram_app.add_handler(MessageHandler(filters.ALL, master_message_handler))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


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
    bc_task = asyncio.create_task(scheduled_broadcast_worker())
    yield
    ping_task.cancel()
    bc_task.cancel()
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
