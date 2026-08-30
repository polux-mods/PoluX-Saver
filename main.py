import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from yt_dlp import YoutubeDL

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_PLAYLIST_ITEMS = 15

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in Render Environment Variables.")
if not PUBLIC_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is not available.")

def is_youtube_url(url):
    return bool(re.match(r"^https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/", url, re.I))

def is_youtube_music_url(url):
    return bool(re.match(r"^https?://music\.youtube\.com/", url, re.I))

def extract_info(url):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def download_audio(url, workdir):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    opts = {
        "format": "bestaudio/best", "outtmpl": output, "noplaylist": True,
        "quiet": True, "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp3 = str(Path(filename).with_suffix(".mp3"))
        if os.path.exists(mp3):
            return mp3, info
        candidates = list(Path(workdir).glob("*.mp3"))
        if candidates:
            return str(candidates[0]), info
        raise FileNotFoundError("FFmpeg did not create MP3.")

def download_video(url, workdir):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    opts = {
        "format": "best[ext=mp4][height<=720]/bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]/best",
        "outtmpl": output, "merge_output_format": "mp4", "noplaylist": True,
        "quiet": True, "no_warnings": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        mp4 = str(Path(filename).with_suffix(".mp4"))
        if os.path.exists(mp4):
            return mp4, info
        if os.path.exists(filename):
            return filename, info
        candidates = [p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() in {".mp4",".mkv",".webm",".mov"}]
        if candidates:
            return str(candidates[0]), info
        raise FileNotFoundError("Video file was not created.")

def cleanup(workdir):
    shutil.rmtree(workdir, ignore_errors=True)

async def send_audio(context, chat_id, filepath, info):
    size = os.path.getsize(filepath)
    if size > MAX_FILE_SIZE:
        return False, f"Файл має {size/1024/1024:.1f} МБ і перевищує ліміт Telegram."
    title = (info.get("title") or "audio")[:64]
    performer = info.get("artist") or info.get("uploader")
    performer = performer[:64] if performer else None
    duration = info.get("duration")
    with open(filepath, "rb") as f:
        await context.bot.send_audio(chat_id=chat_id, audio=f, filename="audio.mp3",
                                     title=title, performer=performer,
                                     duration=int(duration) if duration else None)
    return True, None

async def send_video(context, chat_id, filepath, info):
    size = os.path.getsize(filepath)
    if size > MAX_FILE_SIZE:
        return False, f"Відео має {size/1024/1024:.1f} МБ і перевищує ліміт Telegram."
    duration = info.get("duration")
    width, height = info.get("width"), info.get("height")
    with open(filepath, "rb") as f:
        await context.bot.send_video(chat_id=chat_id, video=f, filename="video.mp4",
                                     duration=int(duration) if duration else None,
                                     width=int(width) if width else None,
                                     height=int(height) if height else None,
                                     supports_streaming=True)
    return True, None

async def download_and_send(context, chat_id, url, choice, status):
    workdir = tempfile.mkdtemp(prefix="yt_tg_")
    try:
        await status.edit_text("⏳ Отримую інформацію...")
        info = await asyncio.to_thread(extract_info, url)
        if not info:
            raise RuntimeError("YouTube не повернув інформацію.")
        if choice == "audio":
            await status.edit_text("🎵 Завантажую аудіо...")
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            await status.edit_text("📤 Відправляю аудіо...")
            return await send_audio(context, chat_id, filepath, info)
        await status.edit_text("🎬 Завантажую відео...")
        filepath, info = await asyncio.to_thread(download_video, url, workdir)
        await status.edit_text("📤 Відправляю відео...")
        return await send_video(context, chat_id, filepath, info)
    finally:
        cleanup(workdir)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! 👋\n\nНадішли посилання на YouTube або YouTube Music.\nМожна відео, трек або плейлист.")

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    url = update.message.text.strip()
    if not is_youtube_url(url):
        await update.message.reply_text("❌ Надішли коректне посилання YouTube або YouTube Music.")
        return
    context.user_data["url"] = url
    if is_youtube_music_url(url):
        keyboard = [[InlineKeyboardButton("🎵 Завантажити аудіо", callback_data="audio")]]
        text = "🎵 YouTube Music — завантажити як аудіо?"
    else:
        keyboard = [[InlineKeyboardButton("🎵 Аудіо", callback_data="audio"),
                     InlineKeyboardButton("🎬 Відео", callback_data="video")]]
        text = "Обери формат:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    url = context.user_data.get("url")
    if not url:
        await query.edit_message_text("❌ Посилання втрачено. Надішли його ще раз.")
        return
    choice = query.data
    status = await query.edit_message_text("⏳ Перевіряю посилання...")
    try:
        info = await asyncio.to_thread(extract_info, url)
        if info.get("_type") == "playlist" or info.get("entries"):
            entries = [x for x in (info.get("entries") or []) if x][:MAX_PLAYLIST_ITEMS]
            if not entries:
                await status.edit_text("❌ Плейлист порожній або недоступний.")
                return
            await status.edit_text(f"📋 Знайдено {len(entries)} елементів. Починаю...")
            success = failed = 0
            for index, entry in enumerate(entries, 1):
                entry_url = entry.get("webpage_url") or entry.get("url")
                if entry_url and not entry_url.startswith("http"):
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"
                if not entry_url:
                    failed += 1
                    continue
                try:
                    await status.edit_text(f"⏳ {index}/{len(entries)} — завантажую...")
                    ok, error = await download_and_send(context, query.message.chat_id, entry_url, choice, status)
                    if ok:
                        success += 1
                    else:
                        failed += 1
                        await context.bot.send_message(query.message.chat_id, f"⚠️ Елемент {index}: {error}")
                except Exception as e:
                    failed += 1
                    await context.bot.send_message(query.message.chat_id, f"⚠️ Елемент {index} не завантажено:\n{str(e)[:500]}")
            await status.edit_text(f"✅ Готово!\n\nУспішно: {success}\nПомилок: {failed}")
            return
        ok, error = await download_and_send(context, query.message.chat_id, url, choice, status)
        if ok:
            await status.delete()
        else:
            await status.edit_text(f"❌ {error}")
    except Exception as e:
        await status.edit_text(f"❌ Помилка під час завантаження:\n{str(e)[:700]}")

async def error_handler(update, context):
    print("Telegram error:", context.error)

telegram_app = Application.builder().token(TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
telegram_app.add_handler(CallbackQueryHandler(process_download))
telegram_app.add_error_handler(error_handler)

app = FastAPI()

@app.get("/")
async def root():
    return PlainTextResponse("YouTube Telegram Bot is running.")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided != WEBHOOK_SECRET:
            return PlainTextResponse("Unauthorized", status_code=401)
    update = Update.de_json(await request.json(), telegram_app.bot)
    asyncio.create_task(telegram_app.process_update(update))
    return PlainTextResponse("OK")

@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = f"{PUBLIC_URL}/telegram/webhook"
    await telegram_app.bot.set_webhook(url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    print(f"Webhook set: {webhook_url}")

@app.on_event("shutdown")
async def shutdown():
    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
