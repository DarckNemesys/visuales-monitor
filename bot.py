#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import zipfile
import subprocess
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ChatAction

# ========== CONFIGURACIÓN ==========
TOKEN = "8723078700:AAGr_-qY3zhbXBRlxS-6aLWR6hQ8_O-fTDY"  # ← CAMBIA ESTO POR TU TOKEN REAL

# Directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
COMPRIMIDOS_DIR = os.path.join(BASE_DIR, "comprimidos")
PARTES_DIR = os.path.join(BASE_DIR, "partes")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

for d in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "bot.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constantes
LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

# ========== FUNCIONES ==========
def get_file_size(path):
    return os.path.getsize(path)

def compress_folder(folder_path, output_name):
    """Comprime una carpeta a ZIP"""
    output_path = os.path.join(COMPRIMIDOS_DIR, output_name)
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, folder_path)
                zipf.write(file_path, arcname)
    return output_path

def split_file(file_path):
    """Divide archivo usando split"""
    base_name = os.path.basename(file_path)
    output_pattern = os.path.join(PARTES_DIR, f"{base_name}.part")
    part_size = TAMANO_PARTE_MB * 1024 * 1024
    
    cmd = f"split -b {part_size} '{file_path}' '{output_pattern}'"
    subprocess.run(cmd, shell=True, check=True)
    
    parts = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) 
                    if f.startswith(base_name + ".part")])
    return parts

def descargar_archivo(url, destino):
    """Descarga un archivo"""
    nombre = os.path.basename(url)
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    
    ruta = os.path.join(destino, nombre)
    response = requests.get(url, stream=True)
    response.raise_for_status()
    
    with open(ruta, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return ruta

def listar_contenido_carpeta(url):
    """Lista archivos en carpeta web"""
    try:
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        enlaces = []
        for a in soup.find_all('a'):
            href = a.get('href')
            if href and href != '../' and not href.startswith('?'):
                if not href.endswith('/'):  # Solo archivos, no subcarpetas
                    full_url = url.rstrip('/') + '/' + href
                    enlaces.append(full_url)
        return enlaces
    except Exception as e:
        logger.error(f"Error listando carpeta: {e}")
        return []

# ========== FUNCIONES DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot Visuales UCLV*\n\n"
        "📌 *Comandos:*\n"
        "/descargar `<url>` - Descargar archivo\n"
        "/estado - Estado del bot\n"
        "/limpiar - Limpiar temporales\n"
        "/ayuda - Esta ayuda\n\n"
        "_Los archivos >2GB se dividen automáticamente_",
        parse_mode="Markdown"
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Ayuda*\n\n"
        "`/descargar https://oops.uclv.edu.cu/archivo.mp4`\n\n"
        "✅ Archivos <2GB → envío directo\n"
        "✂️ Archivos >2GB → partes de 1.9GB\n"
        "📁 Carpetas → comprimidas en ZIP",
        parse_mode="Markdown"
    )

async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uso = 0
    archivos = 0
    for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            fp = os.path.join(dir_path, f)
            if os.path.isfile(fp):
                uso += os.path.getsize(fp)
                archivos += 1
    
    await update.message.reply_text(
        f"📊 *Estado*\n\n"
        f"✅ Activo\n"
        f"💾 {uso/(1024**3):.2f} GB usados\n"
        f"📁 {archivos} archivos temporales",
        parse_mode="Markdown"
    )

async def limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eliminados = 0
    for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            os.remove(os.path.join(dir_path, f))
            eliminados += 1
    await update.message.reply_text(f"🧹 Limpiados {eliminados} archivos")

async def descargar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Uso: `/descargar <URL>`", parse_mode="Markdown")
        return
    
    url = context.args[0]
    msg = await update.message.reply_text(f"🔄 *Procesando...*", parse_mode="Markdown")
    
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
        
        # Verificar si es carpeta o archivo
        if url.endswith('/'):
            await msg.edit_text("📁 *Carpeta detectada*\nListando contenido...", parse_mode="Markdown")
            archivos = listar_contenido_carpeta(url)
            
            if not archivos:
                await msg.edit_text("❌ No se encontraron archivos", parse_mode="Markdown")
                return
            
            await msg.edit_text(f"📥 Descargando {len(archivos)} archivos...")
            descargados = []
            for i, file_url in enumerate(archivos):
                await msg.edit_text(f"📥 ({i+1}/{len(archivos)}): {os.path.basename(file_url)}", parse_mode="Markdown")
                archivo_path = descargar_archivo(file_url, DESCARGAS_DIR)
                descargados.append(archivo_path)
            
            await msg.edit_text("🗜️ Comprimiendo...")
            zip_name = f"carpeta_{int(time.time())}.zip"
            zip_path = compress_folder(DESCARGAS_DIR, zip_name)
            
            for f in descargados:
                os.remove(f)
            
            tamaño = get_file_size(zip_path)
            
            if tamaño <= LIMITE_2GB:
                await msg.edit_text(f"📤 Subiendo ({tamaño/(1024**2):.1f}MB)...")
                await context.bot.send_document(chat_id=update.effective_chat.id, document=open(zip_path, 'rb'))
                await msg.delete()
            else:
                await msg.edit_text(f"✂️ Dividiendo ({tamaño/(1024**3):.2f}GB)...")
                partes = split_file(zip_path)
                for i, parte in enumerate(partes, 1):
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=open(parte, 'rb'),
                        caption=f"📦 Parte {i}/{len(partes)}"
                    )
                await msg.delete()
        
        else:  # Es archivo
            await msg.edit_text("📥 *Descargando archivo...*", parse_mode="Markdown")
            archivo_path = descargar_archivo(url, DESCARGAS_DIR)
            tamaño = get_file_size(archivo_path)
            
            if tamaño <= LIMITE_2GB:
                await msg.edit_text(f"📤 Subiendo ({tamaño/(1024**2):.1f}MB)...")
                await context.bot.send_document(chat_id=update.effective_chat.id, document=open(archivo_path, 'rb'))
                await msg.delete()
            else:
                await msg.edit_text(f"✂️ Archivo de {tamaño/(1024**3):.2f}GB\nDividiendo...", parse_mode="Markdown")
                partes = split_file(archivo_path)
                for i, parte in enumerate(partes, 1):
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=open(parte, 'rb'),
                        caption=f"✂️ Parte {i}/{len(partes)}"
                    )
                await msg.delete()
        
        # Limpiar archivos temporales después de enviar
        for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
            for f in os.listdir(dir_path):
                os.remove(os.path.join(dir_path, f))
                
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.edit_text(f"❌ *Error:* `{str(e)[:150]}`", parse_mode="Markdown")

# ========== MAIN ==========
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("limpiar", limpiar))
    app.add_handler(CommandHandler("descargar", descargar))
    
    print("="*50)
    print("🤖 BOT INICIADO")
    print("="*50)
    
    app.run_polling()

if __name__ == "__main__":
    main()