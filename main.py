import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

# Ваш токен від BotFather
TOKEN = "8787993439:AAFeVmWBRiVvMAlpO4SCnd3mT1Hlohkajxk"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Надішли мені посилання на відео, трек або плейлист з YouTube / YouTube Music 🎵🎬")

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ Будь ласка, надішли дійсне посилання.")
        return

    # Зберігаємо посилання для використання після натискання кнопки
    context.user_data['url'] = url

    # Створюємо кнопки вибору формату
    keyboard = [
        [
            InlineKeyboardButton("🎵 Завантажити Аудіо", callback_data="audio"),
            InlineKeyboardButton("🎬 Завантажити Відео", callback_data="video")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Обери формат завантаження:", reply_markup=reply_markup)

async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    url = context.user_data.get('url')
    if not url:
        await query.edit_message_text("❌ Посилання втрачено. Будь ласка, надішли його знову.")
        return

    format_choice = query.data
    status_msg = await query.edit_message_text("⏳ Завантажую... Якщо це плейлист, файли надсилатимуться по черзі.")

    # Базові налаштування з ОБХОДОМ БЛОКУВАННЯ (зміна клієнта та заголовків)
    ydl_opts = {
        'outtmpl': 'downloads/%(title)s.%(ext)s',
        'noplaylist': False,  # Дозволяємо плейлисти
        'ignoreerrors': False, # Пропускати видалені або приватні відео в плейлистах
        'no_warnings': True,
        'max_filesize': 50 * 1024 * 1024, # Обмеження Telegram у 50 МБ
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'android_embedded', 'ios'],
                'skip': ['hls', 'dash']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }

    # Налаштовуємо формат залежно від вибору користувача
    if format_choice == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    try:
        loop = asyncio.get_running_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
                
        info = await loop.run_in_executor(None, extract)
        
        if not info:
            await status_msg.edit_text("❌ Не вдалося отримати дані. Можливо, контент заблоковано.")
            return

        # Обробляємо як одне відео, так і масив плейлиста
        entries = []
        if 'entries' in info and info['entries'] is not None:
            entries = [e for e in info['entries'] if e is not None]
        else:
            entries = [info]

        if not entries:
            await status_msg.edit_text("❌ Не знайдено доступних файлів для завантаження.")
            return

        # Шукаємо та відправляємо всі завантажені файли
        files_sent = 0
        for entry in entries:
            title = entry.get('title', 'Media')
            
            # Скануємо папку downloads, знаходимо завантажений файл і відправляємо
            for filename in os.listdir('downloads'):
                file_path = os.path.join('downloads', filename)
                if os.path.isfile(file_path):
                    with open(file_path, 'rb') as file_data:
                        if format_choice == 'audio':
                            await context.bot.send_audio(chat_id=query.message.chat_id, audio=file_data, title=title)
                        else:
                            await context.bot.send_video(chat_id=query.message.chat_id, video=file_data, caption=title)
                    os.remove(file_path) # Очищаємо після відправки
                    files_sent += 1

        if files_sent > 0:
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Файл перевищує ліміт Telegram (50 МБ) або сталася помилка конвертації.")

    except Exception as e:
        await status_msg.edit_text(f"❌ Помилка: {str(e)[:150]}")

if __name__ == '__main__':
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    app = Application.builder().token(TOKEN).build()
    
    # Додано обробники для повідомлень та кнопок
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    app.add_handler(CallbackQueryHandler(process_download))
    
    app.run_polling()
    
