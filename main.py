import os
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TOKEN = "8787993439:AAFeVmWBRiVvMAlpO4SCnd3mT1Hlohkajxk"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Надішли мені посилання на YouTube або YouTube Music (відео, трек або плейлист) 🎵🎬")

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ Будь ласка, надішли дійсне посилання.")
        return

    context.user_data['url'] = url
    keyboard = [
        [
            InlineKeyboardButton("🎵 Завантажити Аудіо", callback_data="audio"),
            InlineKeyboardButton("🎬 Завантажити Відео", callback_data="video")
        ]
    ]
    await update.message.reply_text("Обери формат:", reply_markup=InlineKeyboardMarkup(keyboard))

async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    url = context.user_data.get('url')
    if not url:
        await query.edit_message_text("❌ Посилання втрачено. Надішли його знову.")
        return

    format_choice = query.data
    status_msg = await query.edit_message_text("⏳ Шукаю вільний сервер... Зачекайте хвилинку.")

    def fetch_media():
        # Список публічних серверів Cobalt, які НЕ вимагають авторизації (без JWT)
        instances = [
            "https://co.eepy.today/",
            "https://cobalt.owo.vc/",
            "https://api.cobalt.qwyku.com/",
            "https://cobalt.kwiatektv.me/"
        ]
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        
        payload = {
            "url": url,
            "isAudioOnly": True if format_choice == "audio" else False,
            "audioFormat": "mp3"
        }
        
        # Бот по черзі стукає в сервери. Знаходить перший робочий — бере дані.
        for api_url in instances:
            try:
                response = requests.post(api_url, json=payload, headers=headers, timeout=15)
                if response.status_code == 200:
                    return response.json()
            except Exception:
                continue # Якщо сервер недоступний, пробуємо наступний
                
        return {"error": {"code": "Усі безкоштовні сервери наразі перевантажені. Спробуйте через хвилину."}}

    try:
        data = await asyncio.to_thread(fetch_media)
        
        # Перевірка на внутрішні помилки сервера
        if "error" in data:
            err_text = data.get("error", {}).get("code", "Невідома помилка")
            await status_msg.edit_text(f"❌ Помилка сервісу: {err_text}")
            return

        status = data.get("status")

        # Успішне отримання прямого посилання на файл
        if status in ["tunnel", "redirect"]:
            file_url = data.get("url")
            filename = "audio.mp3" if format_choice == "audio" else "video.mp4"
            
            await status_msg.edit_text("⏳ Файл знайдено! Завантажую та відправляю у Telegram...")

            file_req = await asyncio.to_thread(requests.get, file_url)
            file_bytes = file_req.content

            if len(file_bytes) > 50 * 1024 * 1024:
                await status_msg.edit_text("❌ Файл перевищує 50 МБ (обмеження Telegram).")
                return

            if format_choice == "audio":
                await context.bot.send_audio(chat_id=query.message.chat_id, audio=file_bytes, filename=filename)
            else:
                await context.bot.send_video(chat_id=query.message.chat_id, video=file_bytes, filename=filename)
            
            await status_msg.delete()

        # Обробка плейлистів
        elif status == "picker":
            items = data.get("picker", [])
            await status_msg.edit_text(f"⏳ Знайдено плейлист ({len(items)} елементів). Надсилаю перші 15 треків...")
            
            for item in items[:15]:
                item_url = item.get("url")
                if item_url:
                    f_req = await asyncio.to_thread(requests.get, item_url)
                    f_bytes = f_req.content
                    if len(f_bytes) <= 50 * 1024 * 1024:
                        if format_choice == "audio":
                            await context.bot.send_audio(chat_id=query.message.chat_id, audio=f_bytes)
                        else:
                            await context.bot.send_video(chat_id=query.message.chat_id, video=f_bytes)
            await status_msg.delete()

        else:
            await status_msg.edit_text("❌ Не вдалося обробити посилання. Можливо, воно не підтримується.")

    except Exception as e:
        await status_msg.edit_text(f"❌ Сталася помилка: {str(e)[:150]}")

if __name__ == '__main__':
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    app.add_handler(CallbackQueryHandler(process_download))
    app.run_polling()
        
