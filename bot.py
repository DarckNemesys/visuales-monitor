#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de Telegram para Visuales UCLV - Optimizado
Integra descarga de archivos por streaming, emparejamiento y segmentación nativa.
"""

import os
import re
import time
import json
import logging
import urllib.parse
import requests
import threading
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

# Redirección e infraestructura UCLV
URL_BASE_ESPEJO = "https://oops.uclv.edu.cu/"
TAMANO_PARTE_MB = 1900  # Límite seguro inferior a 2GB (Telegram)

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
        """Traduce enlaces de visuales.uclv.cu al mirror accesible oops.uclv.edu.cu"""
        if not url:
            return ""
        url_corregida = url.replace("https://visuales.uclv.cu", "https://oops.uclv.edu.cu")
        url_corregida = url_corregida.replace("http://visuales.uclv.cu", "https://oops.uclv.edu.cu")
        return url_corregida

    @staticmethod
    def construir_url_completa(base: str, href: str) -> str:
        return urllib.parse.urljoin(base, href)

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
        logger.error(f"El archivo no existe para enviar: {archivo_path}")
        return False
    try:
        with open(archivo_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
            r = requests.post(url, data=data, files=files, timeout=300) # Timeout extendido para subidas
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando documento {archivo_path}: {e}")
        return False

# ========== MOTOR DE SCRAPING OPTIMIZADO ==========
def escanear_directorio_uclv(url: str) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Scrapea de forma estructurada separando Videos, Subtítulos y Carpetas"""
    url = URLUtils.corregir_url(url)
    if not url.endswith('/'):
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
    """Descarga por chunks protegiendo el búfer de memoria RAM del servidor"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(destino_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536): # Bloques estables de 64KB
                    if chunk:
                        f.write(chunk)
                        f.flush() # Forzar volcado a disco inmediato
        return True
    except Exception as e:
        logger.error(f"Fallo en descarga streaming de {url}: {e}")
        if os.path.exists(destino_path):
            os.remove(destino_path)
        return False

def dividir_archivo_nativo(archivo_path: str, tamano_mb: int = TAMANO_PARTE_MB) -> List[str]:
    """Divide archivos grandes de forma binaria pura (Multiplataforma: Windows/Linux)"""
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
        logger.error(f"Error segmentando archivo {archivo_path}: {e}")
    return partes_generadas

# ========== TRABAJADOR EN HILO SECUNDARIO (BACKGROUND WORKER) ==========
def proceso_descarga_y_envio(chat_id: int, url_archivo: str, nombre_archivo: str):
    url_archivo = URLUtils.corregir_url(url_archivo)
    ruta_local = os.path.join(DESCARGAS_DIR, nombre_archivo)
    
    enviar_mensaje(chat_id, f"📥 *Iniciando descarga al servidor:*\n`{nombre_archivo}`\n\n_Por favor, espera..._")
    
    if not descargar_archivo_streaming(url_archivo, rta_local := ruta_local):
        enviar_mensaje(chat_id, f"❌ Error al descargar `{nombre_archivo}` desde el servidor universitario.")
        return

    try:
        tamano_total = os.path.getsize(ruta_local)
        limite_bytes = TAMANO_PARTE_MB * 1024 * 1024
        
        if tamano_total <= limite_bytes:
            enviar_mensaje(chat_id, f"⚡ Descarga completada en servidor. Subiendo a Telegram...")
            if enviar_documento(chat_id, ruta_local, f"✅ `{nombre_archivo}`"):
                enviar_mensaje(chat_id, f"🎉 ¡Envío completado exitosamente!")
            else:
                enviar_mensaje(chat_id, f"❌ Error al subir `{nombre_archivo}` a Telegram.")
        else:
            enviar_mensaje(chat_id, f"📦 El archivo supera los 2GB. Iniciando segmentación binaria nativa...")
            partes = dividir_archivo_nativo(ruta_local)
            
            if not partes:
                enviar_mensaje(chat_id, "❌ Error crítico al intentar dividir el archivo.")
                return
                
            enviar_mensaje(chat_id, f"📤 Subiendo archivo en ({len(partes)}) partes indexadas...")
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"⏳ Subiendo parte {i}/{len(partes)}...")
                enviar_documento(chat_id, parte, f"📦 Parte {i}/{len(partes)} de `{nombre_archivo}`")
                try: os.remove(parte) except: pass
                
            enviar_mensaje(chat_id, f"🎉 ¡Todas las partes de `{nombre_archivo}` fueron enviadas!")
            
    except Exception as e:
        logger.error(f"Error en worker: {e}")
        enviar_mensaje(chat_id, f"❌ Ocurrió un error inesperado procesando el archivo: `{str(e)}`")
    finally:
        if os.path.exists(ruta_local):
            try: os.remove(ruta_local) except: pass

# ========== INTERFAZ DE BOTONES INTERACTIVOS ==========
def construir_teclado_directorio(carpetas: List[Dict], videos: List[Dict], subtitulos: List[Dict]) -> Dict:
    inline_keyboard = []
    
    # Listar carpetas primero
    for c in carpetas[:10]:  # Acotado a 10 para evitar saturar el layout de Telegram
        inline_keyboard.append([{"text": f"📁 {c['nombre']}", "callback_data": f"exp:{c['url'][:50]}"}]) # Callback acotado por bytes
        
    # Listar Videos
    for v in videos[:15]:
        inline_keyboard.append([{"text": f"🎬 {v['nombre']}", "callback_data": f"dl:{v['url'][-50:]}"}])

    # Listar Subtítulos
    for s in subtitulos[:10]:
        inline_keyboard.append([{"text": f"📝 Sub: {s['nombre']}", "callback_data": f"dl:{s['url'][-50:]}"}])
        
    return {"inline_keyboard": inline_keyboard}

# ========== ENDPOINTS Y WEBHOOK DE FLASK ==========
@app.route('/', methods=['GET'])
def index():
    return "Bot de Almacenamiento y Descargas UCLV Activo", 200

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

@app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook_handler():
    try:
        update = request.get_json()
        if not update:
            return jsonify({"status": "no_data"}), 400
            
        if "message" in update:
            message = update["message"]
            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            
            if text.startswith("/start") or text.startswith("/ayuda"):
                menu = (
                    "👋 *Bienvenido al Gestor y Descargador de Visuales UCLV.*\n\n"
                    "Envía la URL de la carpeta de Visuales que deseas inspeccionar.\n"
                    "El bot corregirá el tráfico de forma interna y te devolverá las opciones de descarga.\n\n"
                    "👉 _Ejemplo de uso:_\n"
                    "`https://visuales.uclv.cu/Peliculas/Extranjeras/2026/`"
                )
                enviar_mensaje(chat_id, menu)
                
            elif "visuales.uclv.cu" in text or "oops.uclv.edu.cu" in text:
                enviar_mensaje(chat_id, "🔍 Analizando directorio remoto...")
                carpetas, videos, subtitulos = escanear_directorio_uclv(text)
                
                if not carpetas and not videos and not subtitulos:
                    enviar_mensaje(chat_id, "⚠️ No se encontraron elementos legibles o el servidor rechazó la conexión.")
                else:
                    markup = construir_teclado_directorio(carpetas, videos, subtitulos)
                    enviar_mensaje(chat_id, f"📂 *Resultados de:* {text}\nSelecciona un archivo para iniciar la descarga automatizada:", reply_markup=markup)
            
            elif text.startswith("/descargar"):
                # Soporte por comando directo: /descargar [url]
                partes_texto = text.split(" ", 1)
                if len(partes_texto) > 1:
                    url_objetivo = partes_texto[1].strip()
                    nombre_archivo = urllib.parse.unquote(url_objetivo.split("/")[-1])
                    if nombre_archivo:
                        threading.Thread(target=proceso_descarga_y_envio, args=(chat_id, url_objetivo, nombre_archivo)).start()
                    else:
                        enviar_mensaje(chat_id, "❌ URL inválida.")
                else:
                    enviar_mensaje(chat_id, "💡 Uso correcto: `/descargar [URL_DEL_ARCHIVO]`")
            else:
                enviar_mensaje(chat_id, "❓ Envía un enlace directo válido de Visuales UCLV para procesarlo.")
                
        elif "callback_query" in update:
            # Procesamiento de botones interactivos
            query = update["callback_query"]
            chat_id = query["message"]["chat"]["id"]
            enviar_mensaje(chat_id, "⚡ Procesando solicitud interactiva de descarga por línea segura...")
            
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error procesando webhook: {e}")
        return jsonify({"status": "error", "details": str(e)}), 500

def set_webhook():
    time.sleep(1)
    url_set = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    try:
        r = requests.get(url_set, timeout=10)
        if r.status_code == 200:
            logger.info(f"Webhook configurado exitosamente en: {WEBHOOK_URL}")
        else:
            logger.error(f"Error configurando Webhook: {r.text}")
    except Exception as e:
        logger.error(f"Fallo de conexión al setear Webhook: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=set_webhook, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
