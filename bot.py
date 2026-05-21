# =============================================
# UCLV Visuales Telegram Bot - Versión Unificada para Render
# Mantengo lógica original de descarga/scraping
# =============================================

import os
import asyncio
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from pathlib import Path

# ====================== CORE ORIGINAL (mínimo intacto) ======================
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
import re

class UCLVDownloader:
    """Clase core original - lógica de scraping y descarga intacta"""
    
    def __init__(self, download_path="downloads"):
        self.download_path = Path(download_path)
        self.download_path.mkdir(exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def get_file_list(self, url: str):
        """Scraping original del sitio"""
        if not url.startswith("http"):
            url = "https://" + url.strip("/")
        
        response = self.session.get(url)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        files = []
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.endswith(('.mp4', '.srt', '.jpg', '.nfo')):
                full_url = url.rstrip('/') + '/' + href.lstrip('/')
                files.append({
                    'name': href,
                    'url': full_url,
                    'type': self._get_type(href)
                })
        return files

    def _get_type(self, filename):
        if filename.endswith('.mp4'): return 'video'
        if filename.endswith('.srt'): return 'subtitle'
        if filename.endswith('.jpg'): return 'image'
        if filename.endswith('.nfo'): return 'info'
        return 'other'

    async def download_file(self, file_url: str, filename: str, progress_callback=None):
        """Descarga con progreso (lógica base preservada)"""
        filepath = self.download_path / filename
        
        response = self.session.get(file_url, stream=True)
        total_size = int(response.headers.get('content-length', 0))
        
        with open(filepath, 'wb') as f, tqdm(
            desc=filename[:30],
            total=total_size,
            unit='B',
            unit_scale=True,
            unit_divisor=1024,
            disable=True  # Se controla desde Telegram
        ) as bar:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    if progress_callback:
                        await progress_callback(len(chunk))
                    bar.update(len(chunk))
        return filepath


# ====================== BOT ======================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
DOWNLOAD_PATH = "downloads"

app = FastAPI()
downloader = UCLVDownloader(DOWNLOAD_PATH)

# Application de telegram
tg_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **Bot Visuales UCLV**\n\n"
        "Envía la URL de una carpeta de `visuales.uclv.cu` para empezar."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "visuales.uclv.cu" not in url:
        await update.message.reply_text("❌ Solo URLs de visuales.uclv.cu")
        return

    msg = await update.message.reply_text("🔍 Analizando carpeta...")

    try:
        files = downloader.get_file_list(url)
        context.user_data['current_url'] = url
        context.user_data['files'] = files

        text = f"✅ **{len(files)} archivos encontrados**\n\n¿Qué deseas descargar?"

        keyboard = [
            [InlineKeyboardButton("📥 Todo", callback_data="download_all")],
            [InlineKeyboardButton("🎥 Solo Videos + SRT", callback_data="videos_srt")],
            [InlineKeyboardButton("🎥 Solo Videos", callback_data="videos_only")],
        ]

        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await msg.edit_text(f"❌ Error al analizar:\n{str(e)}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    files = context.user_data.get('files', [])

    if not files:
        await query.edit_message_text("❌ Sesión expirada. Envía la URL nuevamente.")
        return

    filter_type = None
    if data == "videos_only":
        filter_type = 'video'
    elif data == "videos_srt":
        filter_type = ['video', 'subtitle']

    to_download = files
    if filter_type:
        to_download = [f for f in files if f['type'] in (filter_type if isinstance(filter_type, list) else [filter_type])]

    await query.edit_message_text(f"📥 Iniciando descarga de **{len(to_download)}** archivos...")

    # Descarga en background
    asyncio.create_task(background_download(query, to_download))


async def background_download(query, files):
    """Descarga en segundo plano"""
    success = 0
    for file in files:
        try:
            await query.message.reply_text(f"⬇️ Descargando: {file['name']}")
            await downloader.download_file(
                file['url'],
                file['name']
            )
            success += 1
        except Exception as e:
            await query.message.reply_text(f"❌ Falló {file['name']}: {str(e)[:100]}")

    await query.message.reply_text(f"✅ **Descarga finalizada**\n{success}/{len(files)} archivos descargados.")


def setup_handlers(application: Application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & \~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_callback))


# ====================== FASTAPI ======================
@app.post(f"/{TOKEN}")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return {"status": "error"}

@app.get("/")
async def health():
    return {"status": "✅ UCLV Telegram Bot running on Render", "downloads": len(list(Path(DOWNLOAD_PATH).glob("*")))}

@app.on_event("startup")
async def startup():
    await tg_app.initialize()
    setup_handlers(tg_app)
    await tg_app.start()
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'tu-proyecto.onrender.com')}/{TOKEN}"
    await tg_app.bot.set_webhook(webhook_url)
    print(f"Webhook configurado: {webhook_url}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
