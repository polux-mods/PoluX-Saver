import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# ⚠️ ВСТАВТЕ СВІЙ ТОКЕН ВІД BOTFATHER МІЖ КУПЮРАМИ ⚠️
TOKEN = "8787993439:AAFeVmWBRiVvMAlpO4SCnd3mT1Hlohkajxk"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Надішли мені посилання на YouTube-відео або плейлист, і я скачаю аудіо для тебе!")

async def download_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("Будь ласка, надішли дійсне посилання.")
        return

    status_msg = await update.message.reply_text("⏳ Завантажую аудіо... Це може зайняти від 10 секунд до пару хвилин.")

    # Налаштування завантажувача
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'downloads/%(title)s.%(ext)s',
        'noplaylist': False,
    }

    try:
        loop = asyncio.get_running_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
                
        info = await loop.run_in_executor(None, extract)
        
        # Обробка: як для одного треку, так і для плейлистів
        entries = info.get('entries', [info]) if 'entries' in info else [info]
        
        for entry in entries:
            filename = ydl.prepare_filename(entry)
            if os.path.exists(filename):
                with open(filename, 'rb') as audio:
                    await update.message.reply_audio(audio=audio, title=entry.get('title', 'Audio'))
                os.remove(filename)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ Помилка: {str(e)[:150]}")

if __name__ == '__main__':
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_audio))
    app.run_polling()
    
