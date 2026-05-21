#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de Telegram para Visuales UCLV
Adaptación directa y fiel del repositorio uclv_downloader.
No se altera la lógica de scraping, extensiones ni el motor de descargas.
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
import uuid
from pathlib import Path
from typing import Set, List, Dict, Any, Tuple, Optional, Callable
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

# ========== CONFIGURACIÓN DE ENTORNO ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN no configurado en las variables de entorno")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL", "https://visuales-bot.onrender.com"))
if WEBHOOK_URL.endswith('/'):
    WEBHOOK_URL = WEBHOOK_URL[:-1]

LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

# Directorios de trabajo
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
PARTES_DIR = os.path.join(BASE_DIR, "partes")

for d in [DESCARGAS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Diccionario seguro para almacenar los mapeos de botones inline
temp_urls = {}

# =====================================================================
# CLASES Y LÓGICA COPIADAS EXACTAMENTE DEL REPOSITORIO UCLV_DOWNLOADER
# =====================================================================

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

def scrape_folder(url: str, recursive: bool = False, max_depth: int = 1) -> List[Dict]:
    items = []
    if not url.endswith('/'):
        url += '/'
        
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

def descargar_archivo(url: str, destino: str, chat_id: Optional[int] = None,
                      progress_callback: Optional[Callable] = None,
                      max_retries: int = 3) -> Tuple[str, int]:
    
    nombre = os.path.basename(urllib.parse.unquote(url.split('?')[0]))
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
        
    nombre = FileUtils.clean_filename(nombre)
    ruta = os.path.join(destino, nombre)
    
    for intento in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)'}
            response = requests.get(url, stream=True, timeout=120, headers=headers)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            content_type = response.headers.get('content-type', '')
            
            if 'text/html' in content_type and total_size < 150000:
                if 'visuales.uclv.cu' in url:
                    url_alternativa = url.replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
                    return descargar_archivo(url_alternativa, destino, chat_id, progress_callback, max_retries)
                raise Exception("El servidor devolvió un HTML de error o denegación de acceso en lugar del archivo.")
                
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
            logger.error(f"Intento {intento + 1} fallido: {e}")
            if intento == max_retries - 1:
                raise e
            time.sleep(4)
            
    raise Exception("No se pudo descargar el archivo tras agotar reintentos nativos.")

# =====================================================================
# UTILERÍAS COMPLEMENTARIAS PARA EL ENTREGABLE DE TELEGRAM
# =====================================================================

def es_carpeta(url: str) -> bool:
    url_limpia = url.strip().split('?')[0]
    if url_limpia.endswith('/'):
        return True
    ultimo_segmento = url_limpia.split('/')[-1]
    if '.' not in ultimo_segmento:
        return True
    ext = Path(ultimo_segmento).suffix.lower()
    if ext in ['.html', '.htm', '.php']:
        return True
    return False

def dividir_archivo_nativamente(archivo_path: str) -> List[str]:
    parte_size = TAMANO_PARTE_MB * 1024 * 1024
    base = os.path.basename(archivo_path)
    patron = os.path.join(PARTES_DIR, f"{base}.part")
    subprocess.run(f"split -b {parte_size} '{archivo_path}' '{patron}'", shell=True, check=True)
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) if f.startswith(base + ".part")])
    return partes

def limpiar_directorios_temporales():
    for ruta_dir in [DESCARGAS_DIR, PARTES_DIR]:
        if os.path.exists(ruta_dir):
            for f in os.listdir(ruta_dir):
                try:
                    os.remove(os.path.join(ruta_dir, f))
                except Exception as e:
                    logger.error(f"No se pudo borrar {f}: {e}")

# =====================================================================
# MÉTODOS DE COMUNICACIÓN CON LA API DE TELEGRAM
# =====================================================================

def enviar_mensaje(chat_id: int, texto: str, reply_markup: Optional[Dict] = None) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return requests.post(url, json=payload, timeout=15).ok
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        return False

def enviar_documento(chat_id: int, archivo_path: str, caption: str = "") -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(archivo_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id, 'caption': caption[:1024], 'parse_mode': 'Markdown'}
            return requests.post(url, data=data, files=files, timeout=300).ok
    except Exception as e:
        logger.error(f"Error enviando documento: {e}")
        return False

def responder_callback(callback_id: str, texto: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_id, "text": texto}, timeout=10)
    except Exception as e:
        logger.error(f"Error respondiendo callback: {e}")

# =====================================================================
# CONTROLADORES DE FLUJOS INTERNOS
# =====================================================================

def ejecutar_mapeo_carpeta(chat_id: int, url_carpeta: str):
    global temp_urls
    url_carpeta = url_carpeta.strip()
    if 'visuales.uclv.cu' in url_carpeta:
        url_carpeta = url_carpeta.replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
        
    enviar_mensaje(chat_id, f"🔍 *Analizando directorio remoto con uclv-downloader...*\n`{url_carpeta}`")
    
    items = scrape_folder(url_carpeta, recursive=False, max_depth=0)
    archivos = [item for item in items if item['type'] != 'other']
    
    if not archivos:
        enviar_mensaje(chat_id, "⚠️ No se encontraron archivos legibles en esta ruta o el acceso fue denegado por el servidor.")
        return
        
    iconos = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}
    
    mensaje = f"📁 *Archivos detectados ({len(archivos)}):*\n\n"
    for i, archivo in enumerate(archivos[:20], 1):
        icono = iconos.get(archivo['type'], '📄')
        nombre_visible = urllib.parse.unquote(archivo['name'])
        mensaje += f"{i}. {icono} `{nombre_visible[:55]}`\n"
        
    if len(archivos) > 20:
        mensaje += f"\n... y {len(archivos) - 20} elementos más."
        
    enviar_mensaje(chat_id, mensaje)
    
    inline_keyboard = []
    for archivo in archivos[:12]:
        nombre_corto = urllib.parse.unquote(archivo['name'])[:32]
        callback_id = f"file_{uuid.uuid4().hex[:8]}"
        temp_urls[callback_id] = archivo['url']
        inline_keyboard.append([{"text": f"{iconos.get(archivo['type'], '📄')} {nombre_corto}", "callback_data": callback_id}])
        
    if inline_keyboard:
        enviar_mensaje(chat_id, "📌 *Selecciona un archivo para descargar:*", {"inline_keyboard": inline_keyboard})

def ejecutar_flujo_desc
