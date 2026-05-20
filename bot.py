#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import zipfile
import logging
import subprocess
import requests
import threading
from datetime import datetime
from flask import Flask, request, jsonify

# ========== CONFIGURACIÓN ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN no configurado")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL"))
if not WEBHOOK_URL:
    WEBHOOK_URL = "https://visuales-bot.onrender.com"

LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

# Directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
COMPRIMIDOS_DIR = os.path.join(BASE_DIR, "comprimidos")
PARTES_DIR = os.path.join(BASE_DIR, "partes")

for d in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== FUNCIONES DE TELEGRAM ==========
def enviar_mensaje(chat_id, texto):
    """Envía un mensaje a Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=10)
        logger.info(f"Mensaje enviado a {chat_id}: {response.status_code}")
        return response.ok
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        return False

def enviar_documento(chat_id, archivo_path, caption=""):
    """Envía un documento a Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(archivo_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id, 'caption': caption[:1024]}
            response = requests.post(url, data=data, files=files, timeout=120)
            return response.ok
    except Exception as e:
        logger.error(f"Error enviando documento: {e}")
        return False

def set_webhook():
    """Configura el webhook en Telegram"""
    webhook_url = f"{WEBHOOK_URL}/webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
    try:
        response = requests.get(url, timeout=10)
        result = response.json()
        if result.get('ok'):
            logger.info(f"Webhook configurado: {webhook_url}")
        else:
            logger.error(f"Error webhook: {result}")
        return result
    except Exception as e:
        logger.error(f"Error en setWebhook: {e}")
        return None

# ========== FUNCIONES DE DESCARGA ==========
def descargar_archivo(url, destino):
    """Descarga un archivo desde URL"""
    nombre = os.path.basename(url)
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    ruta = os.path.join(destino, nombre)
    
    logger.info(f"Descargando {url}")
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    
    with open(ruta, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return ruta

def dividir_archivo(archivo_path):
    """Divide un archivo usando split de Linux"""
    parte_size = TAMANO_PARTE_MB * 1024 * 1024
    base = os.path.basename(archivo_path)
    patron = os.path.join(PARTES_DIR, f"{base}.part")
    
    logger.info(f"Dividiendo {archivo_path}")
    subprocess.run(f"split -b {parte_size} '{archivo_path}' '{patron}'", shell=True, check=True)
    
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) 
                     if f.startswith(base + ".part")])
    return partes

def limpiar_temporales():
    """Limpia todos los directorios temporales"""
    for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            try:
                os.remove(os.path.join(dir_path, f))
            except:
                pass
    logger.info("Archivos temporales limpiados")

def procesar_descarga(chat_id, url):
    """Procesa una solicitud de descarga"""
    try:
        enviar_mensaje(chat_id, f"🔄 *Procesando:* `{url[:80]}...`")
        
        enviar_mensaje(chat_id, "📥 Descargando archivo...")
        archivo = descargar_archivo(url, DESCARGAS_DIR)
        tamaño = os.path.getsize(archivo)
        
        if tamaño <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo archivo... ({tamaño/(1024**2):.1f}MB)")
            if enviar_documento(chat_id, archivo, f"✅ {os.path.basename(archivo)}"):
                enviar_mensaje(chat_id, "✅ *Descarga completada*")
            else:
                enviar_mensaje(chat_id, "❌ Error al subir el archivo")
        else:
            enviar_mensaje(chat_id, f"✂️ Archivo de {tamaño/(1024**3):.2f}GB, dividiendo...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Subiendo parte {i}/{len(partes)}")
                enviar_documento(chat_id, parte, f"📦 {os.path.basename(archivo)} - Parte {i}/{len(partes)}")
            enviar_mensaje(chat_id, f"✅ *Descarga completada*")
        
    except Exception as e:
        logger.error(f"Error en descarga: {e}")
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
    finally:
        limpiar_temporales()

# ========== SERVIDOR FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot activo", "version": "1.0"}

@app.route('/health')
def health():
    return {"status": "healthy"}

@app.route('/webhook', methods=['POST'])
def webhook():
    """Recibe y procesa mensajes de Telegram"""
    try:
        update = request.get_json()
        logger.info(f"Webhook recibido: {update}")
        
        # Procesar mensaje
        if 'message' in update:
            msg = update['message']
            chat_id = msg['chat']['id']
            
            # Obtener texto del mensaje
            text = msg.get('text', '')
            if not text:
                return jsonify({"status": "ok"})
            
            text = text.strip()
            logger.info(f"Comando: '{text}' de chat {chat_id}")
            
            # ========== COMANDOS ==========
            if text == '/start':
                enviar_mensaje(chat_id, 
                    "🤖 *Bot de Visuales UCLV*\n\n"
                    "📌 *Comandos:*\n"
                    "`/descargar <url>` - Descargar archivo\n"
                    "`/estado` - Ver estado del bot\n"
                    "`/limpiar` - Limpiar archivos temporales\n"
                    "`/ayuda` - Mostrar ayuda\n\n"
                    "💡 *Ejemplo:*\n"
                    "`/descargar https://oops.uclv.edu.cu/video.mp4`")
            
            elif text.startswith('/descargar'):
                partes = text.split(maxsplit=1)
                if len(partes) == 2:
                    url = partes[1]
                    enviar_mensaje(chat_id, f"🔄 *Descarga iniciada en segundo plano para:*\n`{url[:80]}`")
                    thread = threading.Thread(target=procesar_descarga, args=(chat_id, url))
                    thread.start()
                else:
                    enviar_mensaje(chat_id, "❌ *Uso correcto:* `/descargar <url>`")
            
            elif text == '/estado':
                uso = 0
                archivos = 0
                for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
                    for f in os.listdir(dir_path):
                        fp = os.path.join(dir_path, f)
                        if os.path.isfile(fp):
                            uso += os.path.getsize(fp)
                            archivos += 1
                enviar_mensaje(chat_id,
                    f"📊 *Estado del bot*\n\n"
                    f"✅ Bot activo\n"
                    f"💾 Espacio usado: {uso/(1024**3):.2f} GB\n"
                    f"📁 Archivos temporales: {archivos}\n"
                    f"📦 Límite sin dividir: 2GB\n"
                    f"✂️ Tamaño de parte: {TAMANO_PARTE_MB}MB")
            
            elif text == '/limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
            
            elif text == '/ayuda':
                enviar_mensaje(chat_id,
                    "📖 *Ayuda del Bot*\n\n"
                    "🔹 `/descargar <url>` - Descarga un archivo\n"
                    "🔹 `/estado` - Ver estado del bot\n"
                    "🔹 `/limpiar` - Limpiar archivos temporales\n"
                    "🔹 `/ayuda` - Mostrar esta ayuda\n\n"
                    "⚙️ *Comportamiento:*\n"
                    "• Archivos <2GB → envío directo\n"
                    "• Archivos >2GB → divididos en partes de 1.9GB\n\n"
                    "📌 *Ejemplo:*\n"
                    "`/descargar https://oops.uclv.edu.cu/video.mp4`")
            
            else:
                enviar_mensaje(chat_id, f"❌ *Comando no reconocido:* `{text}`\nUsa `/ayuda` para ver los comandos disponibles.")
        
        return jsonify({"status": "ok"})
    
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

# ========== MAIN ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    
    # Configurar webhook
    time.sleep(2)
    set_webhook()
    
    logger.info(f"Bot iniciado en puerto {port}")
    app.run(host='0.0.0.0', port=port)