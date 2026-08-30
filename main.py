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
    status_msg = await query.edit_message_text("⏳ Обробка через онлайн-шлюз... Зачекайте хвилинку.")

    def fetch_media():
        api_url = "https://api.cobalt.tools/"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        payload = {
            "url": url,
            "downloadMode": "audio" if format_choice == "audio" else "auto",
            "audioFormat": "mp3"
        }
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
        return response.json()

    try:
        data = await asyncio.to_thread(fetch_media)
        status = data.get("status")

        if status in ["tunnel", "redirect"]:
            file_url = data.get("url")
            filename = "audio.mp3" if format_choice == "audio" else "video.mp4"
            
            await status_msg.edit_text("⏳ Завантажую та відправляю файл у Telegram...")

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

        elif status == "picker":
            items = data.get("picker", [])
            await status_msg.edit_text(f"⏳ Знайдено плейлист ({len(items)} елементів). Надсилаю по черзі...")
            
            for item in items[:15]:  # Обмеження перші 15 треків
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
            err_text = data.get("text", "Не вдалося отримати пряме посилання.")
            await status_msg.edit_text(f"❌ Помилка сервісу: {err_text}")

    except Exception as e:
        await status_msg.edit_text(f"❌ Помилка: {str(e)[:150]}")

if __name__ == '__main__':
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    app.add_handler(CallbackQueryHandler(process_download))
    app.run_polling()
    
