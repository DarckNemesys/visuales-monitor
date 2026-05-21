#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de Telegram para Visuales UCLV - Solución de URLs Rota y Archivos Directos
"""

import os
import re
import time
import json
import logging
import urllib.parse
import requests
import threading
import uuid
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

# ========== CONFIGURACIÓN DE LOGS ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== CONFIGURACIÓN GLOBAL ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN no configurado en las variables de entorno")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL", "https://visuales-bot.onrender.com"))
if WEBHOOK_URL.endswith('/'):
    WEBHOOK_URL = WEBHOOK_URL[:-1]

URL_BASE_ESPEJO = "https://oops.uclv.edu.cu/"
TAMANO_PARTE_MB = 1900  

# Almacenamiento seguro temporal para los botones de Telegram (Evita truncar URLs)
CALLBACK_MAP = {}

# Directorios de trabajo
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
PARTES_DIR = os.path.join(BASE_DIR, "partes")

for d in [DESCARGAS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)

# ========== CLASES DE SOPORTE Y UTILIDADES ==========
class Extensiones:
    VIDEOS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv')
    SUBTITULOS = ('.srt', '.vtt', '.ass')
    AUDIO = ('.mp3', '.m4a', '.aac', '.flac')

class URLUtils:
    @staticmethod
    def corregir_url(url: str) -> str:
        if not url:
            return ""
        url_corregida = url.replace("https://visuales.uclv.cu", "https://oops.uclv.edu.cu")
        url_corregida = url_corregida.replace("http://visuales.uclv.cu", "https://oops.uclv.edu.cu")
        return url_corregida

    @staticmethod
    def construir_url_completa(base: str, href: str) -> str:
        return urllib.parse.urljoin(base, href)

    @staticmethod
    def es_archivo_directo(url: str) -> bool:
        path = urllib.parse.urlparse(url).path
        return path.lower().endswith(Extensiones.VIDEOS + Extensiones.SUBTITULOS + Extensiones.AUDIO)

# ========== LÓGICA DE TELEGRAM (API) ==========
def enviar_mensaje(chat_id: int, texto: str, reply_markup: Optional[Dict] = None) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        return False

def enviar_documento(chat_id: int, archivo_path: str, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    if not os.path.exists(archivo_path):
        return False
    try:
        with open(archivo_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
            r = requests.post(url, data=data, files=files, timeout=300)
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando documento: {e}")
        return False

# ========== MOTOR DE SCRAPING OPTIMIZADO ==========
def escanear_directorio_uclv(url: str) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    url = URLUtils.corregir_url(url)
    if not url.endswith('/') and not URLUtils.es_archivo_directo(url):
        url += '/'

    carpetas, videos, subtitulos = [], [], []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Error escaneando URL {url}: {e}")
        return [], [], []

    soup = BeautifulSoup(r.text, 'html.parser')
    for tag in soup.find_all('a'):
        href = tag.get('href')
        if not href or href in ['../', './'] or href.startswith('?'):
            continue
        
        nombre = urllib.parse.unquote(href)
        url_completa = URLUtils.construir_url_completa(url, href)
        
        if href.endswith('/'):
            carpetas.append({'nombre': nombre, 'url': url_completa})
        elif href.lower().endswith(Extensiones.VIDEOS):
            videos.append({'nombre': nombre, 'url': url_completa})
        elif href.lower().endswith(Extensiones.SUBTITULOS):
            subtitulos.append({'nombre': nombre, 'url': url_completa})

    return carpetas, videos, subtitulos

# ========== MOTOR DE DESCARGA Y SEGMENTACIÓN NATIVA ==========
def descargar_archivo_streaming(url: str, destino_path: str) -> bool:
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(destino_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        f.flush()
        return True
    except Exception as e:
        logger.error(f"Fallo en descarga de {url}: {e}")
        if os.path.exists(destino_path):
            try: os.remove(destino_path)
            except Exception: pass
        return False

def dividir_archivo_nativo(archivo_path: str, tamano_mb: int = TAMANO_PARTE_MB) -> List[str]:
    chunk_size = tamano_mb * 1024 * 1024
    base_name = os.path.basename(archivo_path)
    partes_generadas = []
    try:
        with open(archivo_path, 'rb') as f_in:
            contador = 1
            while True:
                datos = f_in.read(chunk_size)
                if not datos:
                    break
                nombre_parte = os.path.join(PARTES_DIR, f"{base_name}.part{contador:03d}")
                with open(nombre_parte, 'wb') as f_out:
                    f_out.write(datos)
                partes_generadas.append(nombre_parte)
                contador += 1
    except Exception as e:
        logger.error(f"Error segmentando: {e}")
    return partes_generadas

def proceso_descarga_y_envio(chat_id: int, url_archivo: str, nombre_archivo: str):
    url_archivo = URLUtils.corregir_url(url_archivo)
    ruta_local = os.path.join(DESCARGAS_DIR, nombre_archivo)
    
    enviar_mensaje(chat_id, f"📥 *Iniciando descarga al servidor:*\n`{nombre_archivo}`")
    
    if not descargar_archivo_streaming(url_archivo, ruta_local):
        enviar_mensaje(chat_id, f"❌ Error al descargar de la UCLV.")
        return

    try:
        tamano_total = os.path.getsize(ruta_local)
        limite_bytes = TAMANO_PARTE_MB * 1024 * 1024
        
        if tamano_total <= limite_bytes:
            enviar_mensaje(chat_id, f"⚡ Subiendo a Telegram...")
            if enviar_documento(chat_id, ruta_local, f"✅ `{nombre_archivo}`"):
                enviar_mensaje(chat_id, f"🎉 ¡Completado!")
            else:
                enviar_mensaje(chat_id, f"❌ Error al subir.")
        else:
            enviar_mensaje(chat_id, f"📦 Supera 2GB. Dividiendo archivo...")
            partes = dividir_archivo_nativo(ruta_local)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"⏳ Subiendo parte {i}/{len(partes)}...")
                enviar_documento(chat_id, parte, f"📦 Parte {i}/{len(partes)} de `{nombre_archivo}`")
                try: os.remove(parte)
                except Exception: pass
            enviar_mensaje(chat_id, f"🎉 ¡Todas las partes enviadas!")
    except Exception as e:
        enviar_mensaje(chat_id, f"❌ Error: `{str(e)}`")
    finally:
        if os.path.exists(ruta_local):
            try: os.remove(ruta_local)
            except Exception: pass

# ========== INTERFAZ DE BOTONES INTERACTIVOS (CON MAPEO) ==========
def construir_teclado_directorio(carpetas: List[Dict], videos: List[Dict], subtitulos: List[Dict]) -> Dict:
    inline_keyboard = []
    
    for c in carpetas[:15]:
        id_unico = f"dir_{uuid.uuid4().hex[:10]}"
        CALLBACK_MAP[id_unico] = c['url']
        inline_keyboard.append([{"text": f"📁 {c['nombre']}", "callback_data": id_unico}])
        
    for v in videos[:20]:
        id_unico = f"dl_{uuid.uuid4().hex[:10]}"
        CALLBACK_MAP[id_unico] = v['url']
        inline_keyboard.append([{"text": f"🎬 {v['nombre']}", "callback_data": id_unico}])

    for s in subtitulos[:10]:
        id_unico = f"dl_{uuid.uuid4().hex[:10]}"
        CALLBACK_MAP[id_unico] = s['url']
        inline_keyboard.append([{"text": f"📝 Sub: {s['nombre']}", "callback_data": id_unico}])
        
    return {"inline_keyboard": inline_keyboard}

# ========== ENDPOINTS Y WEBHOOKS ==========
@app.route('/', methods=['GET'])
def index(): return "Bot Activo", 200

@app.route('/health', methods=['GET'])
def health(): return "OK", 200

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook_handler():
    try:
        update = request.get_json()
        if not update: return jsonify({"status": "no_data"}), 400
            
        if "message" in update:
            message = update["message"]
            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            
            if text.startswith("/start") or text.startswith("/ayuda"):
                enviar_mensaje(chat_id, "👋 Envía un enlace de carpeta o archivo directo de Visuales UCLV.")
                
            elif "visuales.uclv.cu" in text or "oops.uclv.edu.cu" in text:
                # DETECCIÓN DE ARCHIVO DIRECTO
                if URLUtils.es_archivo_directo(text):
                    nombre = urllib.parse.unquote(text.split("/")[-1])
                    threading.Thread(target=proceso_descarga_y_envio, args=(chat_id, text, nombre)).start()
                else:
                    # ES UNA CARPETA
                    enviar_mensaje(chat_id, "🔍 Analizando directorio remoto...")
                    if not text.endswith('/'): text += '/'
                    carpetas, videos, subtitulos = escanear_directorio_uclv(text)
                    
                    if not carpetas and not videos and not subtitulos:
                        enviar_mensaje(chat_id, "⚠️ No se encontraron elementos legibles. Verifica la URL.")
                    else:
                        markup = construir_teclado_directorio(carpetas, videos, subtitulos)
                        enviar_mensaje(chat_id, "📂 Contenido disponible:", reply_markup=markup)
                        
        elif "callback_query" in update:
            query = update["callback_query"]
            chat_id = query["message"]["chat"]["id"]
            callback_data = query.get("data")
            
            url_real = CALLBACK_MAP.get(callback_data)
            if url_real:
                if callback_data.startswith("dir_"):
                    carpetas, videos, subtitulos = escanear_directorio_uclv(url_real)
                    markup = construir_teclado_directorio(carpetas, videos, subtitulos)
                    enviar_mensaje(chat_id, f"📂 Abriendo subcarpeta...", reply_markup=markup)
                elif callback_data.startswith("dl_"):
                    nombre = urllib.parse.unquote(url_real.split("/")[-1])
                    threading.Thread(target=proceso_descarga_y_envio, args=(chat_id, url_real, nombre)).start()
            else:
                enviar_mensaje(chat_id, "❌ Sesión del botón expirada. Por favor, vuelve a enviar la URL.")
                
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"status": "error"}), 500

def set_webhook():
    time.sleep(1)
    requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}/{TELEGRAM_TOKEN}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=set_webhook, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
