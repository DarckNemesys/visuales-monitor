#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, urllib.parse, subprocess, requests, threading, uuid, logging
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN: raise Exception("TELEGRAM_TOKEN faltante")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL", "https://visuales-bot.onrender.com")).rstrip('/')
LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
PARTES_DIR = os.path.join(BASE_DIR, "partes")
for d in [DESCARGAS_DIR, PARTES_DIR]: os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
temp_urls = {}

class FileUtils:
    VIDEO_EXT = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'}
    SUB_EXT = {'.srt', '.sub', '.vtt', '.ass', '.ssa'}
    IMG_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.bmp'}
    INF_EXT = {'.nfo', '.txt', '.info'}
    
    @classmethod
    def get_file_type(cls, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext in cls.VIDEO_EXT: return "video"
        if ext in cls.SUB_EXT: return "subtitle"
        if ext in cls.IMG_EXT: return "image"
        if ext in cls.INF_EXT: return "info"
        return "other"
    
    @staticmethod
    def format_file_size(size_bytes: int) -> str:
        if size_bytes == 0: return "0 B"
        names = ["B", "KB", "MB", "GB"]
        i = 0
        size = float(size_bytes)
        while size >= 1024.0 and i < len(names) - 1:
            size /= 1024.0
            i += 1
        return f"{size:.1f} {names[i]}"

def scrape_folder(url: str) -> list:
    items = []
    if not url.endswith('/'): url += '/'
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        res.raise_for_status()
    except Exception: return items
    
    soup = BeautifulSoup(res.text, 'html.parser')
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href in ['../', './'] or href.startswith('?'): continue
        full_url = urllib.parse.urljoin(url, href)
        if not href.endswith('/'):
            items.append({'name': href, 'url': full_url, 'type': FileUtils.get_file_type(href)})
    return items

def descargar_archivo(url: str, destino: str, progress_callback=None) -> tuple:
    nombre = os.path.basename(urllib.parse.unquote(url.split('?')[0]))
    if not nombre or '.' not in nombre: nombre = f"descarga_{int(time.time())}"
    nombre = re.sub(r'[<>:"/\\|?*]', '_', urllib.parse.unquote(nombre))
    ruta = os.path.join(destino, nombre)
    
    for intento in range(3):
        try:
            res = requests.get(url, stream=True, timeout=120, headers={'User-Agent': 'Mozilla/5.0'})
            res.raise_for_status()
            total_size = int(res.headers.get('content-length', 0))
            if 'text/html' in res.headers.get('content-type', '') and total_size < 150000:
                if 'visuales.uclv.cu' in url:
                    return descargar_archivo(url.replace('visuales.uclv.cu', 'oops.uclv.edu.cu'), destino, progress_callback)
                raise Exception("HTML de error detectado")
            
            downloaded = 0
            with open(ruta, 'wb') as f:
                for chunk in res.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0: progress_callback(downloaded, total_size)
            return ruta, total_size
        except Exception as e:
            if intento == 2: raise e
            time.sleep(4)

def limpiar_directorios_temporales():
    for d in [DESCARGAS_DIR, PARTES_DIR]:
        for f in os.listdir(d):
            try: os.remove(os.path.join(d, f))
            except: pass

def enviar_msg(chat_id: int, text: str, markup: dict = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    p = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    if markup: p["reply_markup"] = markup
    try: requests.post(url, json=p, timeout=15)
    except: pass

def enviar_doc(chat_id: int, path: str, cap: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, 'rb') as f:
            return requests.post(url, data={'chat_id': chat_id, 'caption': cap[:1024], 'parse_mode': 'Markdown'}, files={'document': f}, timeout=300).ok
    except: return False

def ejecutar_flujo(chat_id: int, url: str):
    url = url.strip().replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
    es_carpeta = url.split('?')[0].endswith('/') or '.' not in url.split('/')[-1]
    
    if es_carpeta:
        enviar_msg(chat_id, f"🔍 *Analizando directorio remoto...*\n`{url}`")
        archivos = [i for i in scrape_folder(url) if i['type'] != 'other']
        if not archivos:
            enviar_msg(chat_id, "⚠️ No se encontraron archivos válidos.")
            return
        
        msg = f"📁 *Archivos ({len(archivos)}):*\n\n"
        iconos = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}
        for i, a in enumerate(archivos[:20], 1):
            msg += f"{i}. {iconos.get(a['type'], '📄')} `{urllib.parse.unquote(a['name'])[:55]}`\n"
        enviar_msg(chat_id, msg)
        
        kb = []
        for a in archivos[:12]:
            cid = f"file_{uuid.uuid4().hex[:8]}"
            temp_urls[cid] = a['url']
            kb.append([{"text": f"{iconos.get(a['type'], '📄')} {urllib.parse.unquote(a['name'])[:32]}", "callback_data": cid}])
        if kb: enviar_msg(chat_id, "📌 *Selecciona un archivo:*", {"inline_keyboard": kb})
    else:
        try:
            enviar_msg(chat_id, "📥 *Descargando archivo al servidor...*")
            def progreso(d, t):
                p = (d / t) * 100
                if int(p) % 20 == 0 and d > 0:
                    enviar_msg(chat_id, f"📥 Descarga: {p:.0f}% ({FileUtils.format_file_size(d)} de {FileUtils.format_file_size(t)})")
            
            archivo, tam = descargar_archivo(url, DESCARGAS_DIR, progreso)
            if tam <= LIMITE_2GB:
                enviar_msg(chat_id, f"📤 Subiendo a Telegram: `{os.path.basename(archivo)}`")
                enviar_doc(chat_id, archivo, f"✅ `{os.path.basename(archivo)}`")
            else:
                enviar_msg(chat_id, "✂️ Excede 2GB. Fraccionando binario...")
                base = os.path.basename(archivo)
                subprocess.run(f"split -b {TAMANO_PARTE_MB}M '{archivo}' '{os.path.join(PARTES_DIR, base)}.part'", shell=True, check=True)
                partes = sorted([os.path.join(PARTES_DIR, pt) for pt in os.listdir(PARTES_DIR) if pt.startswith(base + ".part")])
                for i, p in enumerate(partes, 1):
                    enviar_msg(chat_id, f"📤 Enviando parte {i}/{len(partes)}...")
                    enviar_doc(chat_id, p, f"📦 Parte {i}/{len(partes)} - `{base}`")
        except Exception as e:
            enviar_msg(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
        finally:
            limpiar_directorios_temporales()

app = Flask(__name__)

@app.route('/')
def index(): return {"status": "running"}

@app.route('/webhook', methods=['POST'])
def webhook_handler():
    global temp_urls
    update = request.get_json()
    if not update: return jsonify({"status": "empty"}), 400
    
    if 'callback_query' in update:
        cb = update['callback_query']
        if cb['data'] in temp_urls:
            url = temp_urls[cb['data']]
            try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb['id'], "text": "🔄 Procesando..."})
            except: pass
            del temp_urls[cb['data']]
            threading.Thread(target=ejecutar_flujo, args=(cb['message']['chat']['id'], url)).start()
        return jsonify({"status": "ok"})
        
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        text = msg.get('text', '').strip()
        if not text: return jsonify({"status": "ok"})
        
        if text == '/start':
            enviar_msg(chat_id, "🤖 *UCLV PRO Bot*\nEnvía una URL de visuales directamente.", {"keyboard": [["🧹 Limpiar"]], "resize_keyboard": True})
        elif text == '🧹 Limpiar':
            limpiar_directorios_temporales()
            enviar_msg(chat_id, "🧹 Servidor limpio.")
        elif text.startswith('http'):
            threading.Thread(target=ejecutar_flujo, args=(chat_id, text)).start()
            
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}/webhook", timeout=10), daemon=True).start()
    app.run(host='0.0.0.0', port=puerto)
