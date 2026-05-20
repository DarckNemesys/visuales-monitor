#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de Telegram para Visuales UCLV
Integra descarga de archivos y monitoreo de la web
Detecta carpetas y lista su contenido automáticamente
Corrige URLs de visuales.uclv.cu a oops.uclv.edu.cu
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

# ========== UTILIDADES ==========
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

# ========== CORRECCIÓN DE URLS ==========
def corregir_url_archivo(url: str) -> str:
    """
    Corrige URLs de visuales.uclv.cu a oops.uclv.edu.cu
    También maneja URLs que parecen archivos pero están mal formadas
    """
    url_original = url
    url = url.strip()
    
    # Si ya es oops, devolver igual
    if 'oops.uclv.edu.cu' in url:
        return url
    
    # Reemplazar visuales por oops
    if 'visuales.uclv.cu' in url:
        url = url.replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
        logger.info(f"URL corregida: {url_original} -> {url}")
    
    # Decodificar espacios y caracteres especiales
    url = urllib.parse.unquote(url)
    
    return url

def es_carpeta(url: str) -> bool:
    """Detecta si una URL apunta a una carpeta"""
    url_limpia = url.strip()
    
    # Termina con /
    if url_limpia.endswith('/'):
        return True
    
    # No tiene extensión de archivo conocida
    partes = url_limpia.split('/')
    ultimo = partes[-1]
    
    # Si tiene extensión de video o subtítulo, es archivo
    extensiones_archivo = {'.mp4', '.mkv', '.avi', '.mov', '.srt', '.vtt', '.ass', '.jpg', '.png', '.pdf', '.zip'}
    if any(ultimo.lower().endswith(ext) for ext in extensiones_archivo):
        return False
    
    # Si no tiene punto o tiene punto pero es carpeta
    if '.' not in ultimo:
        return True
    
    # Si tiene extensión pero es de HTML o similar
    if ultimo.endswith('.html') or ultimo.endswith('.htm') or ultimo.endswith('.php'):
        return True
    
    return False

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
            response = requests.post(url, data=data, files=files, timeout=180)
            return response.ok
    except Exception as e:
        logger.error(f"Error enviando documento: {e}")
        return False

def enviar_mensaje_con_botones(chat_id, texto, botones):
    """Envía un mensaje con botones inline"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    keyboard = [[{"text": texto_boton, "callback_data": callback}] for texto_boton, callback in botones]
    
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
        "reply_markup": {"inline_keyboard": keyboard}
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
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

# ========== SCRAPING Y MONITOREO ==========
def scrape_folder(url: str, recursive: bool = False, max_depth: int = 1) -> List[Dict]:
    """Escanea una carpeta y devuelve lista de archivos"""
    items = []
    
    # Asegurar que termina en /
    if not url.endswith('/'):
        url += '/'
    
    # Corregir URL si es necesario
    url = corregir_url_archivo(url)
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Error accediendo a {url}: {e}")
        return items
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href == '../' or href.startswith('?'):
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
    """Lista los archivos dentro de una carpeta y ofrece botones para descargar"""
    global temp_urls
    
    try:
        # Corregir URL
        url_carpeta = corregir_url_archivo(url_carpeta)
        
        # Asegurar que termina en /
        if not url_carpeta.endswith('/'):
            url_carpeta += '/'
        
        enviar_mensaje(chat_id, f"📁 *Explorando carpeta:*\n`{url_carpeta}`")
        
        # Scrapear la carpeta
        items = scrape_folder(url_carpeta, recursive=False, max_depth=0)
        
        # Filtrar solo archivos
        archivos = [item for item in items if item['type'] != 'other']
        
        if not archivos:
            enviar_mensaje(chat_id, "❌ No se encontraron archivos en esta carpeta")
            return
        
        # Iconos por tipo
        iconos = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}
        
        # Construir mensaje
        mensaje = f"📁 *Archivos encontrados ({len(archivos)}):*\n\n"
        for i, archivo in enumerate(archivos[:20], 1):
            icono = iconos.get(archivo['type'], '📄')
            nombre = archivo['name'][:60]
            mensaje += f"{i}. {icono} `{nombre}`\n"
        
        if len(archivos) > 20:
            mensaje += f"\n... y {len(archivos) - 20} más\n"
        
        mensaje += f"\n💡 Selecciona un archivo en los botones de abajo para descargarlo."
        enviar_mensaje(chat_id, mensaje)
        
        # Crear botones para los primeros 10 archivos
        botones = []
        for i, archivo in enumerate(archivos[:10]):
            nombre_corto = archivo['name'][:40]
            callback_id = f"desc_{i}_{int(time.time())}_{hash(archivo['url']) % 10000}"
            temp_urls[callback_id] = archivo['url']
            botones.append((f"{iconos.get(archivo['type'], '📄')} {nombre_corto}", callback_id))
        
        if botones:
            enviar_mensaje_con_botones(chat_id, "📌 *Selecciona un archivo para descargar:*", botones)
        
    except Exception as e:
        logger.error(f"Error listando carpeta: {e}")
        enviar_mensaje(chat_id, f"❌ Error al listar la carpeta: {str(e)[:100]}")

def check_for_changes(url: str, state_file: str) -> Dict:
    """Compara el estado actual con el guardado y devuelve cambios"""
    url_corregida = corregir_url_archivo(url)
    current_items = scrape_folder(url_corregida, recursive=True, max_depth=2)
    current_hash = hashlib.md5(json.dumps(current_items, sort_keys=True).encode()).hexdigest()
    
    try:
        with open(state_file, 'r') as f:
            saved_state = json.load(f)
            saved_hash = saved_state.get('hash')
            saved_items = saved_state.get('items', [])
    except:
        saved_hash = None
        saved_items = []
    
    if current_hash == saved_hash:
        return {'changed': False, 'new_items': [], 'removed_items': [], 'total_items': len(current_items)}
    
    current_urls = {item['url'] for item in current_items}
    saved_urls = {item['url'] for item in saved_items}
    
    new_items = [item for item in current_items if item['url'] not in saved_urls]
    removed_items = [item for item in saved_items if item['url'] not in current_urls]
    
    with open(state_file, 'w') as f:
        json.dump({
            'hash': current_hash,
            'items': current_items,
            'timestamp': datetime.now().isoformat()
        }, f, indent=2)
    
    return {
        'changed': True,
        'new_items': new_items,
        'removed_items': removed_items,
        'total_items': len(current_items)
    }

def monitorear_y_notificar(chat_id: Optional[int] = None) -> Dict:
    """Ejecuta monitoreo y notifica al chat si hay cambios"""
    logger.info("Iniciando monitoreo de visuales.uclv.cu...")
    
    if chat_id:
        enviar_mensaje(chat_id, "🔍 *Escaneando visuales.uclv.cu...*\nEsto puede tomar varios segundos.")
    
    resultado = check_for_changes("https://visuales.uclv.cu/", ESTADO_FILE)
    
    if not resultado['changed']:
        if chat_id:
            enviar_mensaje(chat_id, f"✅ *Sin cambios detectados*\n\n📊 Total de archivos monitoreados: {resultado.get('total_items', 0)}")
        return resultado
    
    if chat_id:
        mensaje = f"📢 *CAMBIOS DETECTADOS en visuales.uclv.cu*\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        
        nuevos = resultado['new_items']
        
        if nuevos:
            mensaje += f"🆕 *Nuevos archivos ({len(nuevos)}):*\n"
            for item in nuevos[:10]:
                icono = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}.get(item['type'], '📄')
                mensaje += f"  {icono} `{item['name'][:50]}`\n"
            if len(nuevos) > 10:
                mensaje += f"  ... y {len(nuevos) - 10} más\n"
        
        mensaje += f"\n💡 Usa `/descargar <url_de_carpeta>` para explorar y descargar archivos"
        enviar_mensaje(chat_id, mensaje)
    
    return resultado

# ========== FUNCIONES DE DESCARGA ==========
def descargar_archivo(url: str, destino: str, chat_id: Optional[int] = None,
                      progress_callback: Optional[Callable] = None,
                      max_retries: int = 3) -> Tuple[str, int]:
    """Descarga un archivo con reintentos y callback de progreso"""
    url_limpia = corregir_url_archivo(url.strip())
    
    # Extraer nombre del archivo
    nombre = os.path.basename(urllib.parse.unquote(url_limpia.split('?')[0]))
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    
    nombre = FileUtils.clean_filename(nombre)
    ruta = os.path.join(destino, nombre)
    
    logger.info(f"Descargando: {url_limpia} -> {ruta}")
    
    for intento in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            response = requests.get(url_limpia, stream=True, timeout=120, headers=headers)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            content_type = response.headers.get('content-type', '')
            if 'text/html' in content_type and total_size < 102400:
                # Si es HTML, intentar con la URL alternativa
                if 'visuales' in url_limpia:
                    url_alternativa = url_limpia.replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
                    logger.info(f"Reintentando con URL alternativa: {url_alternativa}")
                    return descargar_archivo(url_alternativa, destino, chat_id, progress_callback, max_retries)
                raise Exception("La URL devolvió HTML. Usa la URL de la carpeta (terminada en /) para listar los archivos disponibles.")
            
            downloaded = 0
            with open(ruta, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        progress_callback(downloaded, total_size, nombre)
                    
            return ruta, total_size
            
        except Exception as e:
            logger.error(f"Intento {intento + 1} fallido: {e}")
            if intento == max_retries - 1:
                raise e
            time.sleep(3)
    
    raise Exception("No se pudo descargar el archivo después de varios intentos")

def dividir_archivo(archivo_path: str) -> List[str]:
    """Divide un archivo usando split de Linux"""
    parte_size = TAMANO_PARTE_MB * 1024 * 1024
    base = os.path.basename(archivo_path)
    patron = os.path.join(PARTES_DIR, f"{base}.part")
    
    logger.info(f"Dividiendo {archivo_path} en partes de {TAMANO_PARTE_MB}MB")
    subprocess.run(f"split -b {parte_size} '{archivo_path}' '{patron}'", shell=True, check=True)
    
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) 
                     if f.startswith(base + ".part")])
    return partes

def limpiar_temporales():
    for dir_path in [DESCARGAS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            try:
                os.remove(os.path.join(dir_path, f))
            except:
                pass
    logger.info("Archivos temporales limpiados")

def procesar_descarga(chat_id: int, url: str):
    """Procesa una solicitud de descarga - detecta si es carpeta o archivo"""
    
    url = url.strip()
    
    # Detectar si es una carpeta
    if es_carpeta(url):
        listar_archivos_carpeta(chat_id, url)
        return
    
    # Es un archivo, proceder con descarga
    try:
        enviar_mensaje(chat_id, f"🔄 *Descargando archivo...*")
        
        def progreso(downloaded, total, filename):
            if total > 0:
                percent = (downloaded / total) * 100
                if int(percent) % 20 == 0 and downloaded > 0:
                    enviar_mensaje(chat_id, f"📥 Progreso: {percent:.0f}% ({FileUtils.format_file_size(downloaded)})")
        
        archivo, tamaño = descargar_archivo(url, DESCARGAS_DIR, chat_id, progreso)
        
        if tamaño < 10240:
            enviar_mensaje(chat_id, f"❌ Error: Archivo muy pequeño ({FileUtils.format_file_size(tamaño)})\n\n💡 Sugerencia: Usa una URL de carpeta (terminada en /) para listar los archivos disponibles.")
            return
        
        if tamaño <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo archivo... ({FileUtils.format_file_size(tamaño)})")
            if enviar_documento(chat_id, archivo, f"✅ {os.path.basename(archivo)}"):
                enviar_mensaje(chat_id, "✅ *Descarga completada*")
            else:
                enviar_mensaje(chat_id, "❌ Error al subir el archivo")
        else:
            enviar_mensaje(chat_id, f"✂️ Archivo de {FileUtils.format_file_size(tamaño)}, dividiendo...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Subiendo parte {i}/{len(partes)}")
                enviar_documento(chat_id, parte, f"📦 {os.path.basename(archivo)} - Parte {i}/{len(partes)}")
            enviar_mensaje(chat_id, f"✅ *Descarga completada*")
        
    except Exception as e:
        logger.error(f"Error en descarga: {e}")
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:200]}`\n\n💡 *Sugerencia:*\nUsa una URL de carpeta (terminada en /) para ver los archivos disponibles:\n`/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/War%20Machine%202026/`")
    finally:
        limpiar_temporales()

# ========== SERVIDOR FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    estado = {}
    try:
        with open(ESTADO_FILE, 'r') as f:
            estado = json.load(f)
    except:
        pass
    
    return {
        "status": "Bot activo",
        "version": "1.0",
        "ultimo_monitoreo": estado.get('timestamp', 'Nunca'),
        "items_monitoreados": len(estado.get('items', []))
    }

@app.route('/health')
def health():
    return {"status": "healthy"}

@app.route('/webhook', methods=['POST'])
def webhook():
    global temp_urls
    
    try:
        update = request.get_json()
        
        # Procesar callbacks de botones
        if 'callback_query' in update:
            callback = update['callback_query']
            callback_id = callback['id']
            chat_id = callback['message']['chat']['id']
            data = callback['data']
            
            logger.info(f"Callback recibido: {data}")
            
            if data in temp_urls:
                url_descarga = temp_urls[data]
                responder_callback(callback_id, f"🔄 Descargando archivo...")
                del temp_urls[data]
                thread = threading.Thread(target=procesar_descarga, args=(chat_id, url_descarga))
                thread.start()
            else:
                responder_callback(callback_id, "Opción no disponible")
            
            return jsonify({"status": "ok"})
        
        # Procesar mensajes de texto
        if 'message' in update:
            msg = update['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip()
            
            if not text:
                return jsonify({"status": "ok"})
            
            logger.info(f"Comando: {text[:50]} de {chat_id}")
            
            if text == '/start':
                comandos = ["📥 Descargar", "🔍 Monitorear", "📊 Estado", "🧹 Limpiar", "❓ Ayuda"]
                enviar_mensaje_con_teclado(chat_id,
                    "🤖 *Bot de Visuales UCLV*\n\n"
                    "✅ Bot funcionando\n\n"
                    "📌 *Instrucciones:*\n"
                    "• Para **explorar una carpeta** y ver sus archivos:\n"
                    "  `/descargar <url_de_carpeta>`\n"
                    "• Para **descargar un archivo** (después de explorar):\n"
                    "  Usa los botones que aparecen\n\n"
                    "📌 *Ejemplo:*\n"
                    "`/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/War%20Machine%202026/`\n\n"
                    "Usa los botones de abajo:",
                    comandos)
            
            elif text == '📥 Descargar' or text.startswith('/descargar'):
                if text == '📥 Descargar':
                    enviar_mensaje(chat_id, "📥 *Explorar carpeta o descargar archivo*\n\n"
                                         "**Para explorar una carpeta:**\n"
                                         "`/descargar https://visuales.uclv.cu/ruta/de/la/carpeta/`\n\n"
                                         "**Ejemplo real:**\n"
                                         "`/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/War%20Machine%202026/`\n\n"
                                         "⚠️ *Importante:* La URL debe terminar en `/` para explorar carpetas.\n\n"
                                         "El bot listará los archivos y te dará botones para descargarlos.")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        url_comando = partes[1]
                        thread = threading.Thread(target=procesar_descarga, args=(chat_id, url_comando))
                        thread.start()
                        enviar_mensaje(chat_id, "🔄 *Procesando solicitud...*")
                    else:
                        enviar_mensaje(chat_id, "❌ *Uso correcto:*\n"
                                              "`/descargar <url_de_carpeta>`\n\n"
                                              "Ejemplo:\n"
                                              "`/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/War%20Machine%202026/`")
            
            elif text == '🔍 Monitorear' or text == '/monitorear':
                thread = threading.Thread(target=monitorear_y_notificar, args=(chat_id,))
                thread.start()
            
            elif text == '📊 Estado' or text == '/estado':
                uso = 0
                archivos_temp = 0
                for dir_path in [DESCARGAS_DIR, PARTES_DIR]:
                    for f in os.listdir(dir_path):
                        fp = os.path.join(dir_path, f)
                        if os.path.isfile(fp):
                            uso += os.path.getsize(fp)
                            archivos_temp += 1
                
                estado = {}
                try:
                    with open(ESTADO_FILE, 'r') as f:
                        estado = json.load(f)
                except:
                    pass
                
                ultimo_escaneo = estado.get('timestamp', 'Nunca')
                total_items = len(estado.get('items', []))
                
                enviar_mensaje(chat_id,
                    f"📊 *Estado del bot*\n\n"
                    f"✅ Activo\n"
                    f"💾 Espacio usado: {FileUtils.format_file_size(uso)}\n"
                    f"📁 Archivos temp: {archivos_temp}\n"
                    f"🔍 Último monitoreo: {ultimo_escaneo[:19] if ultimo_escaneo != 'Nunca' else 'Nunca'}\n"
                    f"📋 Archivos monitoreados: {total_items}\n"
                    f"📦 Límite: 2GB (partes de {TAMANO_PARTE_MB}MB)\n"
                    f"📂 Modo carpetas: ✅ Activado")
            
            elif text == '🧹 Limpiar' or text == '/limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
            
            elif text == '❓ Ayuda' or text == '/ayuda':
                enviar_mensaje(chat_id,
                    "📖 *Ayuda del Bot*\n\n"
                    "🔹 **Comando principal:**\n"
                    "`/descargar <url_de_carpeta>` - Explora una carpeta y lista sus archivos\n\n"
                    "🔹 **Ejemplo completo:**\n"
                    "1. Explora la carpeta:\n"
                    "   `/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/War%20Machine%202026/`\n"
                    "2. El bot mostrará los archivos disponibles\n"
                    "3. Usa los botones para descargar\n\n"
                    "🔹 **Otros comandos:**\n"
                    "• `/monitorear` - Escanea toda la web por cambios\n"
                    "• `/estado` - Ver estado del bot\n"
                    "• `/limpiar` - Limpia archivos temporales\n"
                    "• `/ayuda` - Esta ayuda\n\n"
                    "⚠️ *Importante:* Las URLs para explorar deben terminar en `/`")
            
            else:
                enviar_mensaje(chat_id, f"❌ *Comando no reconocido:* `{text[:30]}`\n\n"
                                      f"💡 **Prueba esto:**\n"
                                      f"`/descargar https://visuales.uclv.cu/Peliculas/Extranjeras/2026/War%20Machine%202026/`\n\n"
                                      f"Usa `/ayuda` para más información.")
        
        return jsonify({"status": "ok"})
    
    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

# ========== MAIN ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    
    time.sleep(2)
    set_webhook()
    
    logger.info(f"Bot iniciado en puerto {port}")
    logger.info("Características:")
    logger.info("  - Detección automática de carpetas")
    logger.info("  - Listado de archivos con botones")
    logger.info("  - Corrección automática de URLs")
    app.run(host='0.0.0.0', port=port)
