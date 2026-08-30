import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# ⚠️ ВСТАВТЕ СВІЙ ТОКЕН ВІД BOTFATHER ⚠️
TOKEN = "8787993439:AAFeVmWBRiVvMAlpO4SCnd3mT1Hlohkajxk"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Надішли мені посилання на YouTube-відео або плейлист, і я скачаю аудіо!")

async def download_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("Будь ласка, надішли дійсне посилання.")
        return

    status_msg = await update.message.reply_text("⏳ Завантажую аудіо... Це може зайняти трохи часу.")

    # Налаштування завантажувача з обходом блокувань
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'downloads/%(title)s.%(ext)s',
        'noplaylist': False,
        'ignoreerrors': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web']
            }
        },
    }

    try:
        loop = asyncio.get_running_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
                
        info = await loop.run_in_executor(None, extract)
        
        # Перевірка на порожню відповідь від yt-dlp
        if not info:
            await status_msg.edit_text("❌ Не вдалося отримати інформацію про відео. Перевірте посилання.")
            return

        # Захищена перевірка: це плейлист чи окреме відео
        entries = []
        if isinstance(info, dict):
            if 'entries' in info and info['entries'] is not None:
                entries = [e for e in info['entries'] if e is not None]
            else:
                entries = [info]

        if not entries:
            await status_msg.edit_text("❌ Не знайдено доступних треків для завантаження.")
            return

        # Відправка файлів у Telegram
        for entry in entries:
            title = entry.get('title', 'Audio')
            # Шукаємо завантажений файл у папці downloads
            for file in os.listdir('downloads'):
                file_path = os.path.join('downloads', file)
                if os.path.isfile(file_path):
                    with open(file_path, 'rb') as audio:
                        await update.message.reply_audio(audio=audio, title=title)
                    os.remove(file_path) # Видаляємо після відправки

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
    
