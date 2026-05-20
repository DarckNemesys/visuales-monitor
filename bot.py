#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot de Telegram para Visuales UCLV
Integra descarga de archivos y monitoreo de la web
Basado en la lógica de uclv_dowloader
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

# ========== UTILIDADES (de uclv_dowloader) ==========
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

def responder_callback(callback_id, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        payload = {"callback_query_id": callback_id, "text": texto, "show_alert": False}
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        logger.error(f"Error respondiendo callback: {e}")
        return False

# ========== SCRAPING Y MONITOREO (de uclv_dowloader) ==========
def scrape_folder(url: str, recursive: bool = True, max_depth: int = 3) -> List[Dict]:
    """
    Escanea una carpeta recursivamente y devuelve lista de archivos
    Adaptado de uclv_dowloader
    """
    items = []
    
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
            # Es carpeta - recursión
            sub_items = scrape_folder(full_url, recursive, max_depth - 1)
            items.extend(sub_items)
        elif not href.endswith('/'):
            # Es archivo
            file_type = FileUtils.get_file_type(href)
            items.append({
                'name': href,
                'url': full_url,
                'type': file_type,
                'timestamp': datetime.now().isoformat()
            })
    
    return items

def check_for_changes(url: str, state_file: str) -> Dict:
    """Compara el estado actual con el guardado y devuelve cambios"""
    current_items = scrape_folder(url, recursive=True, max_depth=2)
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
        return {'changed': False, 'new_items': [], 'removed_items': []}
    
    # Encontrar nuevos y eliminados
    current_urls = {item['url'] for item in current_items}
    saved_urls = {item['url'] for item in saved_items}
    
    new_items = [item for item in current_items if item['url'] not in saved_urls]
    removed_items = [item for item in saved_items if item['url'] not in current_urls]
    
    # Guardar nuevo estado
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
    
    # Intentar con la URL correcta
    resultado = check_for_changes("https://visuales.uclv.cu/", ESTADO_FILE)
    
    if not resultado['changed']:
        if chat_id:
            enviar_mensaje(chat_id, f"✅ *Sin cambios detectados*\n\n📊 Total de archivos monitoreados: {resultado.get('total_items', 0)}")
        return resultado
    
    # Hay cambios
    if chat_id:
        mensaje = f"📢 *CAMBIOS DETECTADOS en visuales.uclv.cu*\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        
        nuevos = resultado['new_items']
        eliminados = resultado['removed_items']
        
        # Agrupar por tipo
        tipos_nuevos = {}
        for item in nuevos:
            tipo = item['type']
            tipos_nuevos[tipo] = tipos_nuevos.get(tipo, 0) + 1
        
        if nuevos:
            mensaje += f"🆕 *Nuevos archivos ({len(nuevos)}):*\n"
            for tipo, count in tipos_nuevos.items():
                icono = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}.get(tipo, '📄')
                mensaje += f"  {icono} {tipo}: {count}\n"
            mensaje += "\n"
        
        if eliminados:
            mensaje += f"🗑️ *Eliminados ({len(eliminados)}):*\n"
            for item in eliminados[:10]:
                mensaje += f"  • `{item['name'][:50]}`\n"
            if len(eliminados) > 10:
                mensaje += f"  ... y {len(eliminados) - 10} más\n"
        
        mensaje += f"\n💡 Usa `/descargar <url>` para descargar archivos"
        enviar_mensaje(chat_id, mensaje)
    
    return resultado

# ========== FUNCIONES DE DESCARGA (mejoradas con lógica de uclv_dowloader) ==========
def descargar_archivo(url: str, destino: str, chat_id: Optional[int] = None,
                      progress_callback: Optional[Callable] = None,
                      max_retries: int = 3) -> Tuple[str, int]:
    """Descarga un archivo con reintentos y callback de progreso"""
    url_limpia = url.strip()
    
    # Seguir redirecciones si es necesario
    try:
        response_head = requests.head(url_limpia, allow_redirects=True, timeout=10)
        if response_head.status_code == 200:
            url_limpia = response_head.url
    except:
        pass
    
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
            
            # Verificar que no es HTML
            content_type = response.headers.get('content-type', '')
            if 'text/html' in content_type and total_size < 102400:
                raise Exception("La URL devolvió HTML (posiblemente no es un archivo directo)")
            
            downloaded = 0
            with open(ruta, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        progress_callback(downloaded, total_size, nombre)
                    
            return ruta, total_size
            
        except Exception as e:
            logger.error(f"Intento {intento + 1} fallido para {url_limpia}: {e}")
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
    """Procesa una solicitud de descarga"""
    try:
        enviar_mensaje(chat_id, f"🔄 *Procesando descarga...*")
        
        def progreso(downloaded, total, filename):
            if total > 0:
                percent = (downloaded / total) * 100
                if int(percent) % 20 == 0 and downloaded > 0:
                    enviar_mensaje(chat_id, f"📥 Progreso: {percent:.0f}% ({FileUtils.format_file_size(downloaded)})")
        
        archivo, tamaño = descargar_archivo(url, DESCARGAS_DIR, chat_id, progreso)
        
        if tamaño < 10240:
            enviar_mensaje(chat_id, f"❌ Error: Archivo muy pequeño ({FileUtils.format_file_size(tamaño)})\nLa URL puede ser incorrecta.")
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
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:200]}`")
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
    try:
        update = request.get_json()
        
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
                    "1. Para descargar: `/descargar <url>`\n"
                    "2. Para escanear: `/monitorear`\n\n"
                    "📌 *Ejemplo URL válida:*\n"
                    "`https://oops.uclv.edu.cu/Peliculas/video.mp4`\n\n"
                    "Usa los botones de abajo:",
                    comandos)
            
            elif text == '📥 Descargar' or text.startswith('/descargar'):
                if text == '📥 Descargar':
                    enviar_mensaje(chat_id, "📥 *Descargar archivo*\n\nEnvía:\n`/descargar <url_completa>`")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        thread = threading.Thread(target=procesar_descarga, args=(chat_id, partes[1]))
                        thread.start()
                        enviar_mensaje(chat_id, "🔄 *Descarga iniciada en segundo plano...*")
                    else:
                        enviar_mensaje(chat_id, "❌ Uso: `/descargar <url>`")
            
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
                    f"📦 Límite: 2GB (partes de {TAMANO_PARTE_MB}MB)")
            
            elif text == '🧹 Limpiar' or text == '/limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
            
            elif text == '❓ Ayuda' or text == '/ayuda':
                enviar_mensaje(chat_id,
                    "📖 *Ayuda del Bot*\n\n"
                    "🔹 `/descargar <url>` - Descarga un archivo\n"
                    "🔹 `/monitorear` - Escanea la web por cambios\n"
                    "🔹 `/estado` - Ver estado del bot\n"
                    "🔹 `/limpiar` - Limpia archivos temporales\n"
                    "🔹 `/ayuda` - Esta ayuda\n\n"
                    "📌 *URLs válidas:*\n"
                    "• Deben ser de oops.uclv.edu.cu\n"
                    "• Deben terminar en .mp4, .mkv, .pdf, etc.\n\n"
                    "⚙️ *Comportamiento:*\n"
                    "• Archivos <2GB → envío directo\n"
                    "• Archivos >2GB → divididos en partes de 1.9GB")
            
            else:
                enviar_mensaje(chat_id, f"❌ *Comando no reconocido:* `{text[:30]}`\nUsa `/ayuda`")
        
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
    app.run(host='0.0.0.0', port=port)
