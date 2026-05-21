#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de Telegram para Visuales UCLV
Integra descarga de archivos y monitoreo de la web
Lógica de Scraping y Descarga extraída exactamente de uclv_downloader
"""

import os
import re
import time
import json
import hashlib
import logging
import urllib.parse
import subprocess
import requests
import threading
from pathlib import Path
from typing import Set, List, Dict, Any, Tuple, Optional, Callable
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

# ========== CONFIGURACIÓN ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN no configurado")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL"))
if not WEBHOOK_URL:
    WEBHOOK_URL = "https://visuales-bot.onrender.com"

if WEBHOOK_URL.endswith('/'):
    WEBHOOK_URL = WEBHOOK_URL[:-1]

URL_BASE = "https://oops.uclv.edu.cu/"
LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

# Directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
PARTES_DIR = os.path.join(BASE_DIR, "partes")
ESTADO_FILE = os.path.join(BASE_DIR, "estado_visuales.json")

for d in [DESCARGAS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Almacenamiento temporal para URLs de carpetas (para callbacks)
temp_urls = {}

# ========== UTILIDADES EXACTAS DEL REPOSITORIO ==========
class URLUtils:
    @staticmethod
    def is_valid_url(url: str) -> bool:
        return url.startswith(('http://', 'https://'))
    
    @staticmethod
    def extract_folder_name(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path_parts = [part for part in parsed.path.split('/') if part]
        if path_parts:
            folder_name = urllib.parse.unquote(path_parts[-1])
            folder_name = re.sub(r'[<>:"/\\|?*]', '_', folder_name)
            return folder_name
        return "descarga_ucvl"
    
    @staticmethod
    def build_full_url(base_url: str, href: str) -> str:
        if href.startswith('http'):
            return href
        return urllib.parse.urljoin(base_url, href)

class FileUtils:
    VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'}
    SUBTITLE_EXTENSIONS = {'.srt', '.sub', '.vtt', '.ass', '.ssa'}
    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp'}
    INFO_EXTENSIONS = {'.nfo', '.txt', '.info'}
    
    @classmethod
    def get_file_type(cls, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext in cls.VIDEO_EXTENSIONS:
            return "video"
        elif ext in cls.SUBTITLE_EXTENSIONS:
            return "subtitle"
        elif ext in cls.IMAGE_EXTENSIONS:
            return "image"
        elif ext in cls.INFO_EXTENSIONS:
            return "info"
        return "other"
    
    @staticmethod
    def format_file_size(size_bytes: int) -> str:
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        size = float(size_bytes)
        while size >= 1024.0 and i < len(size_names) - 1:
            size /= 1024.0
            i += 1
        return f"{size:.1f} {size_names[i]}"
    
    @staticmethod
    def clean_filename(filename: str) -> str:
        filename = urllib.parse.unquote(filename)
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        return filename

# ========== CORRECCIÓN DE URLS OPTIMIZADA PARA APACHE UCLV ==========
def corregir_url_archivo(url: str) -> str:
    url = url.strip()
    
    # Forzar el espejo oops si viene el dominio viejo
    if 'visuales.uclv.cu' in url:
        url = url.replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
        
    parsed = urllib.parse.urlparse(url)
    
    # Decodificamos para evitar doble codificación y luego codificamos protegiendo las barras '/'
    path_limpio = urllib.parse.unquote(parsed.path)
    path_codificado = urllib.parse.quote(path_limpio, safe='/')
    
    return urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, path_codificado, 
        parsed.params, parsed.query, parsed.fragment
    ))

def es_carpeta(url: str) -> bool:
    url_limpia = url.strip()
    if url_limpia.endswith('/'):
        return True
    
    partes = url_limpia.split('/')
    ultimo = partes[-1]
    
    extensiones_archivo = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.srt', '.vtt', '.ass', '.jpg', '.png', '.pdf', '.zip'}
    if any(ultimo.lower().endswith(ext) for ext in extensiones_archivo):
        return False
    
    if '.' not in ultimo:
        return True
    if ultimo.endswith('.html') or ultimo.endswith('.htm') or ultimo.endswith('.php'):
        return True
    return False

# ========== FUNCIONES DE TELEGRAM ==========
def enviar_mensaje(chat_id, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown", "disable_web_page_preview": True}
        response = requests.post(url, json=payload, timeout=12)
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
        response = requests.post(url, json=payload, timeout=12)
        return response.ok
    except Exception as e:
        logger.error(f"Error enviando teclado: {e}")
        return False

def enviar_documento(chat_id, archivo_path, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(archivo_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id, 'caption': caption[:1024], 'parse_mode': 'Markdown'}
            response = requests.post(url, data=data, files=files, timeout=300)
            return response.ok
    except Exception as e:
        logger.error(f"Error enviando documento: {e}")
        return False

def enviar_mensaje_con_botones(chat_id, texto, botones):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    keyboard = [[{"text": texto_boton, "callback_data": callback}] for texto_boton, callback in botones]
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": keyboard}
    }
    try:
        response = requests.post(url, json=payload, timeout=12)
        return response.ok
    except Exception as e:
        logger.error(f"Error enviando botones: {e}")
        return False

def responder_callback(callback_id, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        payload = {"callback_query_id": callback_id, "text": texto, "show_alert": False}
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        logger.error(f"Error respondiendo callback: {e}")
        return False

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

# ========== LÓGICA DE SCRAPING EXACTA DEL REPOSITORIO (SIN ALTERAR) ==========
def scrape_folder(url: str, recursive: bool = False, max_depth: int = 1) -> List[Dict]:
    items = []
    if not url.endswith('/'):
        url += '/'
    
    url = corregir_url_archivo(url)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)'}
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Error accediendo a {url}: {e}")
        return items
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href in ['../', './'] or href.startswith('?'):
            continue
        
        full_url = URLUtils.build_full_url(url, href)
        
        if href.endswith('/') and recursive and max_depth > 0:
            sub_items = scrape_folder(full_url, recursive, max_depth - 1)
            items.extend(sub_items)
        elif not href.endswith('/'):
            file_type = FileUtils.get_file_type(href)
            items.append({
                'name': href,
                'url': full_url,
                'type': file_type,
                'timestamp': datetime.now().isoformat()
            })
    
    return items

def listar_archivos_carpeta(chat_id: int, url_carpeta: str):
    global temp_urls
    try:
        url_carpeta = corregir_url_archivo(url_carpeta)
        if not url_carpeta.endswith('/'):
            url_carpeta += '/'
        
        enviar_mensaje(chat_id, f"🔍 *Analizando directorio remoto con uclv-downloader...*\n`{url_carpeta}`")
        items = scrape_folder(url_carpeta, recursive=False, max_depth=0)
        archivos = [item for item in items if item['type'] != 'other']
        
        if not archivos:
            enviar_mensaje(chat_id, "⚠️ No se encontraron archivos legibles en esta ruta o el acceso fue denegado.")
            return
        
        iconos = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}
        
        mensaje = f"📁 *Archivos detectados ({len(archivos)}):*\n\n"
        for i, archivo in enumerate(archivos[:20], 1):
            icono = iconos.get(archivo['type'], '📄')
            nombre_legible = urllib.parse.unquote(archivo['name'])[:60]
            mensaje += f"{i}. {icono} `{nombre_legible}`\n"
        
        if len(archivos) > 20:
            mensaje += f"\n... y {len(archivos) - 20} más\n"
        
        mensaje += f"\n💡 Presiona el botón del archivo que deseas descargar localmente."
        enviar_mensaje(chat_id, mensaje)
        
        botones = []
        for i, archivo in enumerate(archivos[:14]):
            nombre_corto = urllib.parse.unquote(archivo['name'])[:35]
            callback_id = f"dl_{hashlib.md5(archivo['url'].encode()).hexdigest()[:8]}"
            temp_urls[callback_id] = archivo['url']
            botones.append((f"{iconos.get(archivo['type'], '📄')} {nombre_corto}", callback_id))
        
        if botones:
            enviar_mensaje_con_botones(chat_id, "📌 *Selecciona una descarga:*", botones)
        
    except Exception as e:
        logger.error(f"Error listando carpeta: {e}")
        enviar_mensaje(chat_id, f"❌ Error al evaluar directorio: {str(e)[:100]}")

# ========== LÓGICA DE DESCARGA EXACTA DEL REPOSITORIO (SIN ALTERAR) ==========
def descargar_archivo(url: str, destino: str, chat_id: Optional[int] = None,
                      progress_callback: Optional[Callable] = None,
                      max_retries: int = 3) -> Tuple[str, int]:
    url_limpia = corregir_url_archivo(url.strip())
    
    nombre = os.path.basename(urllib.parse.unquote(url_limpia.split('?')[0]))
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    
    nombre = FileUtils.clean_filename(nombre)
    ruta = os.path.join(destino, nombre)
    
    for intento in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)'}
            response = requests.get(url_limpia, stream=True, timeout=120, headers=headers)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            content_type = response.headers.get('content-type', '')
            
            if 'text/html' in content_type and total_size < 150000:
                if 'visuales' in url_limpia:
                    url_alternativa = url_limpia.replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
                    return descargar_archivo(url_alternativa, destino, chat_id, progress_callback, max_retries)
                raise Exception("El servidor retornó HTML de denegación en lugar del archivo multimedia.")
            
            downloaded = 0
            with open(ruta, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded, total_size, nombre)
            
            return ruta, total_size
            
        except Exception as e:
            logger.error(f"Intento {intento + 1} fallido de descarga: {e}")
            if intento == max_retries - 1:
                raise e
            time.sleep(4)
            
    raise Exception("Imposible procesar la bajada tras agotar reintentos")

def dividir_archivo(archivo_path: str) -> List[str]:
    parte_size = TAMANO_PARTE_MB * 1024 * 1024
    base = os.path.basename(archivo_path)
    patron = os.path.join(PARTES_DIR, f"{base}.part")
    
    logger.info(f"Dividiendo archivo {archivo_path}")
    subprocess.run(f"split -b {parte_size} '{archivo_path}' '{patron}'", shell=True, check=True)
    
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) 
                     if f.startswith(base + ".part")])
    return partes

def limpiar_temporales():
    for dir_path in [DESCARGAS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            try: os.remove(os.path.join(dir_path, f))
            except: pass
    logger.info("Depósitos temporales vaciados.")

def procesar_descarga(chat_id: int, url: str):
    url = url.strip()
    if es_carpeta(url):
        listar_archivos_carpeta(chat_id, url)
        return
    
    try:
        enviar_mensaje(chat_id, f"📥 *Descargando binario al servidor...*")
        
        def progreso(downloaded, total, filename):
            if total > 0:
                percent = (downloaded / total) * 100
                if int(percent) % 20 == 0 and downloaded > 0:
                    enviar_mensaje(chat_id, f"📥 Descargando: {percent:.0f}% ({FileUtils.format_file_size(downloaded)} de {FileUtils.format_file_size(total)})")
        
        archivo, tamaño = descargar_archivo(url, DESCARGAS_DIR, chat_id, progreso)
        
        if tamaño < 2048:
            enviar_mensaje(chat_id, f"❌ Descarga corrupta. Respuesta inválida del servidor universitario.")
            return
        
        if tamaño <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo a Telegram: `{os.path.basename(archivo)}` ({FileUtils.format_file_size(tamaño)})")
            if enviar_documento(chat_id, archivo, f"✅ `{os.path.basename(archivo)}`"):
                enviar_mensaje(chat_id, "🎉 ¡Completado exitosamente!")
            else:
                enviar_mensaje(chat_id, "❌ Error al transferir el archivo hacia Telegram.")
        else:
            enviar_mensaje(chat_id, f"✂️ Archivo excede el límite permitido ({FileUtils.format_file_size(tamaño)}). Segmentando...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Enviando fragmento {i}/{len(partes)}...")
                enviar_documento(chat_id, parte, f"📦 Parte {i}/{len(partes)} - `{os.path.basename(archivo)}`")
            enviar_mensaje(chat_id, f"🎉 ¡Fraccionamiento enviado con éxito!")
            
    except Exception as e:
        logger.error(f"Fallo en descarga: {e}")
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:180]}`\n\n💡 Intenta reenviar el enlace directo asegurando que la carpeta contenga archivos legibles.")
    finally:
        limpiar_temporales()

# ========== SERVIDOR FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot activo", "motor_scraping": "uclv-downloader nativo"}

@app.route('/health')
def health():
    return {"status": "healthy"}, 200

@app.route('/webhook', methods=['POST'])
def webhook():
    global temp_urls
    try:
        update = request.get_json()
        if not update: return jsonify({"status": "no_data"}), 400
        
        if 'callback_query' in update:
            callback = update['callback_query']
            callback_id = callback['id']
            chat_id = callback['message']['chat']['id']
            data = callback['data']
            
            if data in temp_urls:
                url_descarga = temp_urls[data]
                responder_callback(callback_id, f"🔄 Preparando descarga...")
                del temp_urls[data]
                threading.Thread(target=procesar_descarga, args=(chat_id, url_descarga)).start()
            else:
                responder_callback(callback_id, "❌ Botón Expirado. Reenvía el link.")
            return jsonify({"status": "ok"})
        
        if 'message' in update:
            msg = update['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip()
            
            if not text: return jsonify({"status": "ok"})
            
            if text == '/start':
                comandos = ["📥 Descargar", "📊 Estado", "🧹 Limpiar"]
                enviar_mensaje_con_teclado(chat_id,
                    "🤖 *Gestor de Descargas UCLV PRO*\n\n"
                    "Envía un comando `/descargar <url_uclv>` o pega directamente la URL para comenzar.",
                    comandos)
            
            elif text == '📥 Descargar' or text.startswith('/descargar'):
                if text == '📥 Descargar':
                    enviar_mensaje(chat_id, "📥 Envíame el comando seguido de tu enlace:\n\n`/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/`")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        threading.Thread(target=procesar_descarga, args=(chat_id, partes[1])).start()
                    else:
                        enviar_mensaje(chat_id, "⚠️ Formato inválido. Recuerda usar:\n`/descargar <enlace>`")
            
            elif text == '📊 Estado':
                enviar_mensaje(chat_id, "📊 *Estado:* Activo\n⚙️ *Lógica:* Sincronizada con Repositorio Principal")
                
            elif text == '🧹 Limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 Archivos temporales purgados.")
                
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error Webhook: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    time.sleep(1)
    set_webhook()
    app.run(host='0.0.0.0', port=port)
