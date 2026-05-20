#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import logging
import subprocess
import requests
import urllib.parse
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ========== CONFIGURACIÓN ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN no configurado")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL"))
if not WEBHOOK_URL:
    WEBHOOK_URL = "https://visuales-bot.onrender.com"

# ID del canal donde se reflejará el contenido
CANAL_ID = os.environ.get("CANAL_ID") or os.environ.get("CHAT_ID")

LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

# Directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
PARTES_DIR = os.path.join(BASE_DIR, "partes")

for d in [DESCARGAS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== FUNCIONES DE TELEGRAM ==========
def enviar_mensaje(chat_id, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        return False

def enviar_mensaje_con_teclado(chat_id, texto, comandos):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    keyboard = [[{"text": cmd}] for cmd in comandos]
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
        "reply_markup": {"keyboard": keyboard, "resize_keyboard": True}
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        logger.error(f"Error enviando teclado: {e}")
        return False

def enviar_documento(chat_id, archivo_path, caption=""):
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

def enviar_a_canal(texto, archivo=None):
    """Envía contenido al canal (reflejo)"""
    if not CANAL_ID:
        logger.warning("CANAL_ID no configurado")
        return False
    
    if archivo:
        return enviar_documento(CANAL_ID, archivo, texto)
    else:
        return enviar_mensaje(CANAL_ID, texto)

def set_webhook():
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

def formatear_tamaño(tamaño_bytes):
    if tamaño_bytes < 1024:
        return f"{tamaño_bytes} B"
    elif tamaño_bytes < 1024 * 1024:
        return f"{tamaño_bytes/1024:.1f} KB"
    elif tamaño_bytes < 1024 * 1024 * 1024:
        return f"{tamaño_bytes/(1024*1024):.1f} MB"
    else:
        return f"{tamaño_bytes/(1024*1024*1024):.2f} GB"

# ========== FUNCIONES DE DESCARGA ==========
def descargar_archivo(url, destino, chat_id=None):
    """Descarga un archivo desde URL"""
    url_limpia = url.strip()
    
    # Extraer nombre del archivo
    nombre = os.path.basename(urllib.parse.unquote(url_limpia))
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    
    ruta = os.path.join(destino, nombre)
    
    logger.info(f"Descargando: {url_limpia}")
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url_limpia, stream=True, timeout=120, headers=headers)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(ruta, 'wb') as f:
            last_percent = 0
            for chunk in response.iter_content(chunk_size=32768):
                f.write(chunk)
                downloaded += len(chunk)
                if chat_id and total_size > 0:
                    percent = int((downloaded / total_size) * 100)
                    if percent > 0 and percent % 25 == 0 and percent != last_percent:
                        enviar_mensaje(chat_id, f"Progreso: {percent}% ({formatear_tamaño(downloaded)})")
                        last_percent = percent
        
        return ruta, total_size
    
    except Exception as e:
        logger.error(f"Error descargando: {e}")
        raise

def dividir_archivo(archivo_path):
    parte_size = TAMANO_PARTE_MB * 1024 * 1024
    base = os.path.basename(archivo_path)
    timestamp = int(time.time())
    patron = os.path.join(PARTES_DIR, f"{base}_{timestamp}.part")
    
    subprocess.run(['split', '-b', str(parte_size), '-d', archivo_path, patron + '_'], check=True)
    
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) 
                     if f.startswith(f"{base}_{timestamp}.part_")])
    return partes

def limpiar_temporales():
    for dir_path in [DESCARGAS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            try:
                os.remove(os.path.join(dir_path, f))
            except:
                pass

def procesar_descarga(chat_id, url):
    """Procesa descarga y opcionalmente refleja en canal"""
    try:
        enviar_mensaje(chat_id, f"🔄 *Descargando...*")
        
        archivo, tamaño = descargar_archivo(url, DESCARGAS_DIR, chat_id)
        
        if tamaño < 1024:
            enviar_mensaje(chat_id, f"❌ Error: Archivo muy pequeño ({formatear_tamaño(tamaño)})")
            return
        
        # Enviar al usuario
        if tamaño <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo archivo... ({formatear_tamaño(tamaño)})")
            if enviar_documento(chat_id, archivo, f"✅ {os.path.basename(archivo)}"):
                enviar_mensaje(chat_id, "✅ *Completado*")
        else:
            enviar_mensaje(chat_id, f"✂️ Dividiendo archivo...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Subiendo parte {i}/{len(partes)}")
                enviar_documento(chat_id, parte, f"📦 Parte {i}/{len(partes)}")
            enviar_mensaje(chat_id, f"✅ *Completado*")
        
        # REFLEJAR EN EL CANAL (si está configurado)
        if CANAL_ID:
            nombre_archivo = os.path.basename(archivo)
            url_preview = url[:80] + "..." if len(url) > 80 else url
            mensaje_canal = f"📁 *Nuevo archivo reflejado*\n\n📄 `{nombre_archivo}`\n💾 {formatear_tamaño(tamaño)}\n🔗 Fuente: `{url_preview}`"
            
            if tamaño <= LIMITE_2GB:
                enviar_documento(CANAL_ID, archivo, mensaje_canal)
            else:
                enviar_mensaje(CANAL_ID, f"{mensaje_canal}\n\n⚠️ Archivo grande, solicita descarga al bot.")
        
    except Exception as e:
        logger.error(f"Error: {e}")
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
    finally:
        limpiar_temporales()

def reflejar_contenido(chat_id, texto, archivo_url=None):
    """Refleja contenido manualmente en el canal"""
    if not CANAL_ID:
        enviar_mensaje(chat_id, "❌ Canal no configurado. Contacta al administrador.")
        return
    
    if archivo_url:
        enviar_mensaje(chat_id, f"🔄 Reflejando archivo en el canal...")
        try:
            archivo, tamaño = descargar_archivo(archivo_url, DESCARGAS_DIR, chat_id)
            mensaje = f"📁 *{texto}*\n💾 {formatear_tamaño(tamaño)}"
            enviar_documento(CANAL_ID, archivo, mensaje)
            enviar_mensaje(chat_id, f"✅ Reflejado en el canal")
        except Exception as e:
            enviar_mensaje(chat_id, f"❌ Error: {e}")
        finally:
            limpiar_temporales()
    else:
        enviar_mensaje(CANAL_ID, f"📢 *{texto}*")
        enviar_mensaje(chat_id, "✅ Mensaje reflejado en el canal")

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
    try:
        update = request.get_json()
        
        if 'message' in update:
            msg = update['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip()
            
            if not text or not text.strip():
                return jsonify({"status": "ok"})
            
            logger.info(f"Comando: {text[:50]} de {chat_id}")
            
            if text == '/start':
                comandos = ["📥 Descargar", "📢 Reflejar", "📊 Estado", "🧹 Limpiar", "❓ Ayuda"]
                enviar_mensaje_con_teclado(chat_id, 
                    "🤖 *Bot de Visuales UCLV*\n\n"
                    "✅ Bot funcionando\n\n"
                    "📌 *Comandos:*\n"
                    "• `/descargar <url>` - Descarga archivo\n"
                    "• `/reflejar <texto>` - Envía mensaje al canal\n"
                    "• `/reflejar_archivo <texto> <url>` - Refleja archivo\n"
                    "• `/estado` - Ver estado\n"
                    "• `/limpiar` - Limpia temporales\n\n"
                    f"📢 Canal configurado: {'✅ Sí' if CANAL_ID else '❌ No'}",
                    comandos)
            
            elif text == '📥 Descargar' or text.startswith('/descargar'):
                if text == '📥 Descargar':
                    enviar_mensaje(chat_id, "📥 *Descargar*\n\nEnvía:\n`/descargar <url>`")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        thread = threading.Thread(target=procesar_descarga, args=(chat_id, partes[1]))
                        thread.start()
                        enviar_mensaje(chat_id, "🔄 *Descarga iniciada...*")
                    else:
                        enviar_mensaje(chat_id, "❌ Uso: `/descargar <url>`")
            
            elif text == '📢 Reflejar' or text.startswith('/reflejar'):
                if text == '📢 Reflejar':
                    enviar_mensaje(chat_id, "📢 *Reflejar contenido*\n\nPara reflejar un mensaje:\n`/reflejar <texto>`\n\nPara reflejar un archivo:\n`/reflejar_archivo <texto> <url>`")
                elif text.startswith('/reflejar_archivo'):
                    partes = text.split(maxsplit=2)
                    if len(partes) == 3:
                        thread = threading.Thread(target=reflejar_contenido, args=(chat_id, partes[1], partes[2]))
                        thread.start()
                    else:
                        enviar_mensaje(chat_id, "❌ Uso: `/reflejar_archivo <texto> <url>`")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        reflejar_contenido(chat_id, partes[1])
                    else:
                        enviar_mensaje(chat_id, "❌ Uso: `/reflejar <texto>`")
            
            elif text == '📊 Estado' or text == '/estado':
                uso = 0
                archivos_temp = 0
                for dir_path in [DESCARGAS_DIR, PARTES_DIR]:
                    for f in os.listdir(dir_path):
                        fp = os.path.join(dir_path, f)
                        if os.path.isfile(fp):
                            uso += os.path.getsize(fp)
                            archivos_temp += 1
                
                enviar_mensaje(chat_id,
                    f"📊 *Estado*\n\n"
                    f"✅ Activo\n"
                    f"💾 Espacio: {formatear_tamaño(uso)}\n"
                    f"📁 Temp: {archivos_temp}\n"
                    f"📢 Canal: {'✅' if CANAL_ID else '❌'}\n"
                    f"📦 Límite: 2GB")
            
            elif text == '🧹 Limpiar' or text == '/limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Limpiado*")
            
            elif text == '❓ Ayuda' or text == '/ayuda':
                enviar_mensaje(chat_id,
                    "📖 *Ayuda*\n\n"
                    "🔹 `/descargar <url>` - Descarga archivo\n"
                    "🔹 `/reflejar <texto>` - Envía mensaje al canal\n"
                    "🔹 `/reflejar_archivo <texto> <url>` - Refleja archivo\n"
                    "🔹 `/estado` - Ver estado\n"
                    "🔹 `/limpiar` - Limpia temporales\n\n"
                    "📌 *Ejemplo reflejar archivo:*\n"
                    "`/reflejar_archivo Nuevo video! https://ejemplo.com/video.mp4`")
            
            else:
                enviar_mensaje(chat_id, f"❌ Comando no reconocido. Usa /ayuda")
        
        return jsonify({"status": "ok"})
    
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    time.sleep(2)
    set_webhook()
    logger.info(f"Bot iniciado en puerto {port}")
    app.run(host='0.0.0.0', port=port)