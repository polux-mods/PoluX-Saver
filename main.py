import asyncio
import logging
import os
import re
import shutil
import tempfile
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
# CONFIG
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))

MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_PLAYLIST_ITEMS = 15


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# CHECK CONFIG
# =========================================================

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is not configured")


WEBHOOK_URL = f"{PUBLIC_URL}/telegram/webhook"


# =========================================================
# YOUTUBE URL CHECK
# =========================================================

def is_youtube_url(url: str) -> bool:
    pattern = (
        r"^https?://"
        r"(www\.)?"
        r"(youtube\.com|youtu\.be|music\.youtube\.com)/"
    )

    return bool(re.match(pattern, url, re.IGNORECASE))


def is_youtube_music_url(url: str) -> bool:
    return bool(
        re.match(
            r"^https?://music\.youtube\.com/",
            url,
            re.IGNORECASE,
        )
    )


# =========================================================
# YT-DLP INFO
# =========================================================

def extract_info(url: str):

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(
            url,
            download=False,
        )


# =========================================================
# DOWNLOAD AUDIO
# =========================================================

def download_audio(url: str, workdir: str):

    output = str(
        Path(workdir) / "%(title).80s.%(ext)s"
    )

    options = {
        "format": "bestaudio/best",

        "outtmpl": output,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",

                "preferredcodec": "mp3",

                "preferredquality": "128",
            }
        ],
    }

    with YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True,
        )

        filename = ydl.prepare_filename(info)

        mp3_file = str(
            Path(filename).with_suffix(".mp3")
        )

        if os.path.exists(mp3_file):

            return mp3_file, info

        mp3_files = list(
            Path(workdir).glob("*.mp3")
        )

        if mp3_files:

            return str(mp3_files[0]), info

        raise FileNotFoundError(
            "MP3 file was not created."
        )


# =========================================================
# DOWNLOAD VIDEO
# =========================================================

def download_video(url: str, workdir: str):

    output = str(
        Path(workdir) / "%(title).80s.%(ext)s"
    )

    options = {

        "format": (
            "best[ext=mp4][height<=720]/"
            "bestvideo[ext=mp4][height<=720]+"
            "bestaudio[ext=m4a]/"
            "best[height<=720]/"
            "best"
        ),

        "outtmpl": output,

        "merge_output_format": "mp4",

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,
    }

    with YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True,
        )

        filename = ydl.prepare_filename(info)

        mp4_file = str(
            Path(filename).with_suffix(".mp4")
        )

        if os.path.exists(mp4_file):

            return mp4_file, info

        if os.path.exists(filename):

            return filename, info

        video_files = [

            p

            for p in Path(workdir).iterdir()

            if p.is_file()

            and p.suffix.lower()
            in {
                ".mp4",
                ".mkv",
                ".webm",
                ".mov",
            }
        ]

        if video_files:

            return str(video_files[0]), info

        raise FileNotFoundError(
            "Video file was not created."
        )


# =========================================================
# CLEANUP
# =========================================================

def cleanup(workdir: str):

    shutil.rmtree(
        workdir,
        ignore_errors=True,
    )


# =========================================================
# SEND AUDIO
# =========================================================

async def send_audio(
    context,
    chat_id,
    filepath,
    info,
):

    size = os.path.getsize(filepath)

    if size > MAX_FILE_SIZE:

        return (
            False,
            f"Файл має {size / 1024 / 1024:.1f} МБ "
            f"і перевищує ліміт Telegram.",
        )

    title = (
        info.get("title")
        or "audio"
    )[:64]

    performer = (
        info.get("artist")
        or info.get("uploader")
    )

    if performer:

        performer = performer[:64]

    duration = info.get("duration")

    with open(
        filepath,
        "rb",
    ) as audio:

        await context.bot.send_audio(

            chat_id=chat_id,

            audio=audio,

            filename="audio.mp3",

            title=title,

            performer=performer,

            duration=(
                int(duration)
                if duration
                else None
            ),
        )

    return True, None


# =========================================================
# SEND VIDEO
# =========================================================

async def send_video(
    context,
    chat_id,
    filepath,
    info,
):

    size = os.path.getsize(filepath)

    if size > MAX_FILE_SIZE:

        return (
            False,
            f"Відео має {size / 1024 / 1024:.1f} МБ "
            f"і перевищує ліміт Telegram.",
        )

    duration = info.get("duration")

    width = info.get("width")

    height = info.get("height")

    with open(
        filepath,
        "rb",
    ) as video:

        await context.bot.send_video(

            chat_id=chat_id,

            video=video,

            filename="video.mp4",

            duration=(
                int(duration)
                if duration
                else None
            ),

            width=(
                int(width)
                if width
                else None
            ),

            height=(
                int(height)
                if height
                else None
            ),

            supports_streaming=True,
        )

    return True, None


# =========================================================
# DOWNLOAD + SEND
# =========================================================

async def download_and_send(
    context,
    chat_id,
    url,
    choice,
    status_message,
):

    workdir = tempfile.mkdtemp(
        prefix="yt_tg_"
    )

    try:

        await status_message.edit_text(
            "⏳ Отримую інформацію про відео..."
        )

        info = await asyncio.to_thread(
            extract_info,
            url,
        )

        if not info:

            raise RuntimeError(
                "YouTube не повернув інформацію."
            )

        # AUDIO

        if choice == "audio":

            await status_message.edit_text(
                "🎵 Завантажую аудіо..."
            )

            filepath, info = await asyncio.to_thread(
                download_audio,
                url,
                workdir,
            )

            await status_message.edit_text(
                "📤 Відправляю аудіо..."
            )

            return await send_audio(
                context,
                chat_id,
                filepath,
                info,
            )

        # VIDEO

        await status_message.edit_text(
            "🎬 Завантажую відео..."
        )

        filepath, info = await asyncio.to_thread(
            download_video,
            url,
            workdir,
        )

        await status_message.edit_text(
            "📤 Відправляю відео..."
        )

        return await send_video(
            context,
            chat_id,
            filepath,
            info,
        )

    finally:

        cleanup(workdir)


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(

        "Привіт! 👋\n\n"

        "Надішли посилання на YouTube "
        "або YouTube Music.\n\n"

        "Можна відео, трек або плейлист."
    )


# =========================================================
# RECEIVE URL
# =========================================================

async def receive_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:

        return

    if not update.message.text:

        return

    url = update.message.text.strip()

    if not is_youtube_url(url):

        await update.message.reply_text(

            "❌ Надішли коректне посилання "
            "YouTube або YouTube Music."
        )

        return

    context.user_data["url"] = url

    # YOUTUBE MUSIC

    if is_youtube_music_url(url):

        keyboard = [

            [

                InlineKeyboardButton(
                    "🎵 Завантажити аудіо",
                    callback_data="audio",
                )

            ]

        ]

        text = (
            "🎵 YouTube Music\n\n"
            "Завантажити як аудіо?"
        )

    # NORMAL YOUTUBE

    else:

        keyboard = [

            [

                InlineKeyboardButton(
                    "🎵 Аудіо",
                    callback_data="audio",
                ),

                InlineKeyboardButton(
                    "🎬 Відео",
                    callback_data="video",
                ),

            ]

        ]

        text = "Обери формат:"

    await update.message.reply_text(

        text,

        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# =========================================================
# BUTTON
# =========================================================

async def process_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    url = context.user_data.get("url")

    if not url:

        await query.edit_message_text(

            "❌ Посилання втрачено.\n"
            "Надішли його ще раз."
        )

        return

    choice = query.data

    status = await query.edit_message_text(
        "⏳ Перевіряю посилання..."
    )

    try:

        info = await asyncio.to_thread(
            extract_info,
            url,
        )

        # =================================================
        # PLAYLIST
        # =================================================

        if (
            info.get("_type") == "playlist"
            or info.get("entries")
        ):

            entries = [

                item

                for item
                in (info.get("entries") or [])

                if item

            ]

            entries = entries[
                :MAX_PLAYLIST_ITEMS
            ]

            if not entries:

                await status.edit_text(
                    "❌ Плейлист порожній "
                    "або недоступний."
                )

                return

            await status.edit_text(

                f"📋 Знайдено "
                f"{len(entries)} елементів.\n\n"
                f"Починаю завантаження..."
            )

            success = 0

            failed = 0

            for index, entry in enumerate(
                entries,
                start=1,
            ):

                entry_url = (
                    entry.get("webpage_url")
                    or entry.get("url")
                )

                if (
                    entry_url
                    and not entry_url.startswith(
                        "http"
                    )
                ):

                    entry_url = (
                        "https://www.youtube.com/watch?v="
                        + entry_url
                    )

                if not entry_url:

                    failed += 1

                    continue

                try:

                    await status.edit_text(

                        f"⏳ {index}/"
                        f"{len(entries)} "
                        f"— завантажую..."
                    )

                    ok, error = (
                        await download_and_send(

                            context,

                            query.message.chat_id,

                            entry_url,

                            choice,

                            status,
                        )
                    )

                    if ok:

                        success += 1

                    else:

                        failed += 1

                        await context.bot.send_message(

                            chat_id=query.message.chat_id,

                            text=(
                                f"⚠️ Елемент {index}: "
                                f"{error}"
                            ),
                        )

                except Exception as error:

                    failed += 1

                    logger.exception(
                        "Playlist item failed"
                    )

                    await context.bot.send_message(

                        chat_id=query.message.chat_id,

                        text=(
                            f"⚠️ Елемент {index} "
                            f"не завантажено:\n"
                            f"{str(error)[:500]}"
                        ),
                    )

            await status.edit_text(

                f"✅ Готово!\n\n"
                f"Успішно: {success}\n"
                f"Помилок: {failed}"
            )

            return

        # =================================================
        # SINGLE VIDEO
        # =================================================

        ok, error = await download_and_send(

            context,

            query.message.chat_id,

            url,

            choice,

            status,
        )

        if ok:

            await status.delete()

        else:

            await status.edit_text(
                f"❌ {error}"
            )

    except Exception as error:

        logger.exception(
            "Download failed"
        )

        await status.edit_text(

            "❌ Помилка під час завантаження:\n\n"
            f"{str(error)[:1000]}"
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context,
):

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error,
    )


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

telegram_app = (

    Application.builder()

    .token(TOKEN)

    .updater(None)

    .build()
)


telegram_app.add_handler(
    CommandHandler(
        "start",
        start,
    )
)


telegram_app.add_handler(

    MessageHandler(

        filters.TEXT
        & ~filters.COMMAND,

        receive_url,
    )
)


telegram_app.add_handler(

    CallbackQueryHandler(
        process_download
    )
)


telegram_app.add_error_handler(
    error_handler
)


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI()


@app.get("/")
async def root():

    return PlainTextResponse(
        "YouTube Telegram Bot is running."
    )


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


@app.get("/debug/webhook")
async def debug_webhook():

    info = (
        await telegram_app.bot
        .get_webhook_info()
    )

    return {

        "webhook_url": info.url,

        "pending_updates":
            info.pending_update_count,

        "last_error":
            info.last_error_message,

        "last_error_date":
            info.last_error_date,

        "expected_webhook":
            WEBHOOK_URL,
    }


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
):

    # Verify secret

    if WEBHOOK_SECRET:

        provided_secret = (
            request.headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                ""
            )
        )

        if (
            provided_secret
            != WEBHOOK_SECRET
        ):

            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
            )

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot,
    )

    # Put update into PTB queue

    await telegram_app.update_queue.put(
        update
    )

    return PlainTextResponse(
        "OK"
    )


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup():

    logger.info(
        "Starting Telegram application..."
    )

    await telegram_app.initialize()

    await telegram_app.start()

    logger.info(
        "Setting Telegram webhook:"
    )

    logger.info(
        WEBHOOK_URL
    )

    await telegram_app.bot.set_webhook(

        url=WEBHOOK_URL,

        secret_token=(
            WEBHOOK_SECRET
            if WEBHOOK_SECRET
            else None
        ),

        allowed_updates=Update.ALL_TYPES,

        drop_pending_updates=False,
    )

    info = (
        await telegram_app.bot
        .get_webhook_info()
    )

    logger.info(
        "Telegram webhook URL: %s",
        info.url,
    )

    logger.info(
        "Pending updates: %s",
        info.pending_update_count,
    )


# =========================================================
# SHUTDOWN
# =========================================================

@app.on_event("shutdown")
async def shutdown():

    logger.info("Shutting down Telegram application...")

    try:
        await telegram_app.stop()
    finally:
        await telegram_app.shutdown()

    logger.info("Telegram application stopped.")

# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(

        app,

        host="0.0.0.0",

        port=PORT,
            )
