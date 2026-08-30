import os
import re
import asyncio
import tempfile
from pathlib import Path

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


TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to Render Environment Variables.")


# Telegram Bot API cloud limit is currently around 50 MB for normal bot uploads.
# We keep a little safety margin.
MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_PLAYLIST_ITEMS = 15


def is_youtube_url(url: str) -> bool:
    patterns = (
        r"^https?://(www\.)?(youtube\.com|youtu\.be)/",
        r"^https?://music\.youtube\.com/",
    )
    return any(re.match(pattern, url, re.IGNORECASE) for pattern in patterns)


def is_youtube_music_url(url: str) -> bool:
    return bool(re.match(r"^https?://music\.youtube\.com/", url, re.IGNORECASE))


def get_info(url: str):
    """Get basic YouTube metadata without downloading."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def download_audio(url: str, workdir: str):
    """Download best audio and convert to MP3."""
    output = str(Path(workdir) / "%(title).80s.%(ext)s")

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
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp3 = str(Path(filename).with_suffix(".mp3"))

        if os.path.exists(mp3):
            return mp3, info

        # Fallback in case yt-dlp/FFmpeg generated a slightly different path.
        candidates = list(Path(workdir).glob("*.mp3"))
        if candidates:
            return str(candidates[0]), info

        raise FileNotFoundError("MP3 file was not created.")


def download_video(url: str, workdir: str):
    """Download MP4-compatible video, trying to stay under Telegram's limit."""
    output = str(Path(workdir) / "%(title).80s.%(ext)s")

    # Prefer MP4 video/audio. If a combined stream is unavailable,
    # yt-dlp can merge compatible streams through FFmpeg.
    options = {
        "format": (
            "best[ext=mp4][height<=720]/"
            "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/"
            "best[height<=720]/best"
        ),
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

        mp4 = str(Path(filename).with_suffix(".mp4"))
        if os.path.exists(mp4):
            return mp4, info

        if os.path.exists(filename):
            return filename, info

        files = [
            p for p in Path(workdir).iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        ]
        if files:
            return str(files[0]), info

        raise FileNotFoundError("Video file was not created.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! 👋\n\n"
        "Надішли посилання на YouTube або YouTube Music.\n"
        "Можна надіслати відео, трек або плейлист."
    )


async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    if not is_youtube_url(url):
        await update.message.reply_text(
            "❌ Це не схоже на посилання YouTube/YouTube Music.\n"
            "Надішли коректне посилання."
        )
        return

    context.user_data["url"] = url

    # YouTube Music -> automatically audio.
    if is_youtube_music_url(url):
        keyboard = [
            [
                InlineKeyboardButton(
                    "🎵 Завантажити аудіо",
                    callback_data="audio",
                )
            ]
        ]
        text = "🎵 Це посилання YouTube Music. Завантажити як аудіо?"
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    "🎵 Завантажити аудіо",
                    callback_data="audio",
                ),
                InlineKeyboardButton(
                    "🎬 Завантажити відео",
                    callback_data="video",
                ),
            ]
        ]
        text = "Обери формат:"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_audio_file(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    filepath: str,
    info: dict,
):
    size = os.path.getsize(filepath)

    if size > MAX_FILE_SIZE:
        return False, (
            f"❌ Файл занадто великий: {size / 1024 / 1024:.1f} МБ.\n"
            "Telegram не дозволяє надіслати його цим способом."
        )

    title = info.get("title") or "audio"
    performer = info.get("artist") or info.get("uploader") or None
    duration = info.get("duration")

    with open(filepath, "rb") as f:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=f,
            filename="audio.mp3",
            title=title[:64],
            performer=performer[:64] if performer else None,
            duration=int(duration) if duration else None,
        )

    return True, None


async def send_video_file(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    filepath: str,
    info: dict,
):
    size = os.path.getsize(filepath)

    if size > MAX_FILE_SIZE:
        return False, (
            f"❌ Відео занадто велике: {size / 1024 / 1024:.1f} МБ.\n"
            "Спробуй коротше відео або аудіо."
        )

    duration = info.get("duration")
    width = info.get("width")
    height = info.get("height")

    with open(filepath, "rb") as f:
        await context.bot.send_video(
            chat_id=chat_id,
            video=f,
            filename="video.mp4",
            duration=int(duration) if duration else None,
            width=int(width) if width else None,
            height=int(height) if height else None,
            supports_streaming=True,
        )

    return True, None


async def download_single(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    format_choice: str,
    status_message,
):
    workdir = tempfile.mkdtemp(prefix="ytbot_")

    try:
        await status_message.edit_text("⏳ Отримую інформацію про відео...")

        info = await asyncio.to_thread(get_info, url)

        if not info:
            raise RuntimeError("YouTube не повернув інформацію про відео.")

        if format_choice == "audio":
            await status_message.edit_text("🎵 Завантажую аудіо...")
            filepath, downloaded_info = await asyncio.to_thread(
                download_audio, url, workdir
            )

            await status_message.edit_text("📤 Відправляю аудіо...")
            return await send_audio_file(
                context, chat_id, filepath, downloaded_info
            )

        await status_message.edit_text("🎬 Завантажую відео...")
        filepath, downloaded_info = await asyncio.to_thread(
            download_video, url, workdir
        )

        await status_message.edit_text("📤 Відправляю відео...")
        return await send_video_file(
            context, chat_id, filepath, downloaded_info
        )

    finally:
        # Remove temporary downloaded files.
        try:
            for p in Path(workdir).glob("*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            Path(workdir).rmdir()
        except Exception:
            pass


async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    url = context.user_data.get("url")

    if not url:
        await query.edit_message_text(
            "❌ Посилання втрачено. Надішли його ще раз."
        )
        return

    format_choice = query.data
    status_msg = await query.edit_message_text(
        "⏳ Перевіряю посилання..."
    )

    try:
        info = await asyncio.to_thread(get_info, url)

        # Playlist handling.
        if info.get("_type") == "playlist" or info.get("entries"):
            entries = [
                item for item in (info.get("entries") or [])
                if item
            ]

            if not entries:
                await status_msg.edit_text(
                    "❌ Плейлист порожній або недоступний."
                )
                return

            entries = entries[:MAX_PLAYLIST_ITEMS]

            await status_msg.edit_text(
                f"📋 Знайдено {len(entries)} елементів.\n"
                f"Надсилаю по черзі (максимум {MAX_PLAYLIST_ITEMS})."
            )

            success = 0
            failed = 0

            for index, entry in enumerate(entries, start=1):
                entry_url = entry.get("webpage_url") or entry.get("url")

                # extract_flat may return a video ID instead of a full URL.
                if entry_url and not entry_url.startswith("http"):
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"

                if not entry_url:
                    failed += 1
                    continue

                try:
                    await status_msg.edit_text(
                        f"⏳ {index}/{len(entries)} — готую завантаження..."
                    )

                    result, error = await download_single(
                        context,
                        query.message.chat_id,
                        entry_url,
                        format_choice,
                        status_msg,
                    )

                    if result:
                        success += 1
                    else:
                        failed += 1
                        if error:
                            await context.bot.send_message(
                                chat_id=query.message.chat_id,
                                text=f"⚠️ Трек {index}: {error}",
                            )

                except Exception as item_error:
                    failed += 1
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=(
                            f"⚠️ Не вдалося завантажити елемент {index}:\n"
                            f"{str(item_error)[:300]}"
                        ),
                    )

            await status_msg.edit_text(
                f"✅ Плейлист завершено.\n\n"
                f"Успішно: {success}\n"
                f"Помилок: {failed}"
            )
            return

        # Normal single video/track.
        result, error = await download_single(
            context,
            query.message.chat_id,
            url,
            format_choice,
            status_msg,
        )

        if result:
            await status_msg.delete()
        else:
            await status_msg.edit_text(error or "❌ Не вдалося відправити файл.")

    except Exception as e:
        await status_msg.edit_text(
            "❌ Помилка під час завантаження:\n"
            f"{str(e)[:500]}"
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Telegram bot error:", context.error)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
    )
    app.add_handler(
        CallbackQueryHandler(process_download)
    )
    app.add_error_handler(error_handler)

    print("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
