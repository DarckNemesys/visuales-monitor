#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import urllib.parse
import subprocess
import requests
import threading
import uuid
import logging
from flask import Flask, request, jsonify
from pathlib import Path

# Importar el núcleo nativo de uclv_downloader sin alteraciones
import downloader

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN no configurado")

WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL", "https://visuales-bot.onrender.com")).rstrip('/')

LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
PARTES_DIR = os.path.join(BASE_DIR, "partes")

for d in [DESCARGAS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
temp_urls = {}

def es_carpeta(url: str) -> bool:
    url_limpia = url.strip().split('?')[0]
    if url_limpia.endswith('/'): return True
    ultimo = url_limpia.split('/')[-1]
    if '.' not in ultimo or Path(ultimo).suffix.lower() in ['.html', '.htm', '.php']: return True
    return False

def limpiar_directorios_temporales():
    for ruta_dir in [DESCARGAS_DIR, PARTES_DIR]:
        if os.path.exists(ruta_dir):
            for f in os.listdir(ruta_dir):
                try: os.remove(os.path.join(ruta_dir, f))
                except Exception: pass

def enviar_mensaje(chat_id: int, texto: str, markup: dict = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    p = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown", "disable_web_page_preview": True}
    if markup: p["reply_markup"] = markup
    try: requests.post(url, json=p, timeout=15)
    except Exception: pass

def enviar_documento(chat_id: int, path: str, cap: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, 'rb') as f:
            return requests.post(url, data={'chat_id': chat_id, 'caption': cap[:1024], 'parse_mode': 'Markdown'}, files={'document': f}, timeout=300).ok
    except Exception: return False

def responder_callback(cb_id: str, txt: str):
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": txt}, timeout=10)
    except Exception: pass

def ejecutar_mapeo_carpeta(chat_id: int, url: str):
    global temp_urls
    url = url.strip().replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
    enviar_mensaje(chat_id, f"🔍 *Analizando directorio remoto...*\n`{url}`")
    archivos = [i for i in downloader.scrape_folder(url) if i['type'] != 'other']
    
    if not archivos:
        enviar_mensaje(chat_id, "⚠️ No se encontraron archivos legibles.")
        return

    msg = f"📁 *Archivos detectados ({len(archivos)}):*\n\n"
    iconos = {'video': '🎬', 'subtitle': '📝', 'image': '🖼️', 'info': '📄'}
    for i, a in enumerate(archivos[:20], 1):
        msg += f"{i}. {iconos.get(a['type'], '📄')} `{urllib.parse.unquote(a['name'])[:55]}`\n"
    enviar_mensaje(chat_id, msg)
    
    kb = []
    for a in archivos[:12]:
        cid = f"file_{uuid.uuid4().hex[:8]}"
        temp_urls[cid] = a['url']
        kb.append([{"text": f"{iconos.get(a['type'], '📄')} {urllib.parse.unquote(a['name'])[:32]}", "callback_data": cid}])
    if kb: enviar_mensaje(chat_id, "📌 *Selecciona un archivo:*", {"inline_keyboard": kb})

def ejecutar_flujo_descarga(chat_id: int, url: str):
    url = url.strip().replace('visuales.uclv.cu', 'oops.uclv.edu.cu')
    if es_carpeta(url):
        ejecutar_mapeo_carpeta(chat_id, url)
        return
    try:
        enviar_mensaje(chat_id, "📥 *Iniciando descarga al servidor...*")
        def progreso(d, t, f):
            p = (d / t) * 100
            if int(p) % 20 == 0 and d > 0:
                enviar_mensaje(chat_id, f"📥 Descargando: {p:.0f}% ({downloader.FileUtils.format_file_size(d)} de {downloader.FileUtils.format_file_size(t)})")
                
        archivo, tam = downloader.descargar_archivo(url, DESCARGAS_DIR, progreso)
        if tam <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo a Telegram: `{os.path.basename(archivo)}`")
            enviar_documento(chat_id, archivo, f"✅ `{os.path.basename(archivo)}`")
        else:
            enviar_mensaje(chat_id, "✂️ Excede 2GB. Fraccionando binario...")
            base = os.path.basename(archivo)
            subprocess.run(f"split -b {TAMANO_PARTE_MB}M '{archivo}' '{os.path.join(PARTES_DIR, base)}.part'", shell=True, check=True)
            partes = sorted([os.path.join(PARTES_DIR, pt) for pt in os.listdir(PARTES_DIR) if pt.startswith(base + ".part")])
            for i, p in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Enviando parte {i}/{len(partes)}...")
                enviar_documento(chat_id, p, f"📦 Parte {i}/{len(partes)} - `{base}`")
    except Exception as e:
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
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
            responder_callback(cb['id'], "🔄 Descargando...")
            del temp_urls[cb['data']]
            threading.Thread(target=ejecutar_flujo_descarga, args=(cb['message']['chat']['id'], url)).start()
        return jsonify({"status": "ok"})
        
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        text = msg.get('text', '').strip()
        if not text: return jsonify({"status": "ok"})
        
        if text == '/start':
            enviar_mensaje(chat_id, "🤖 *UCLV PRO Bot*\nEnvia la URL directamente o usa `/descargar <url>`", {"keyboard": [["📥 Descargar"], ["🧹 Limpiar"]], "resize_keyboard": True})
        elif text.startswith('/descargar '):
            threading.Thread(target=ejecutar_flujo_descarga, args=(chat_id, text.split(maxsplit=1)[1])).start()
        elif text == '🧹 Limpiar':
            limpiar_directorios_temporales()
            enviar_mensaje(chat_id, "🧹 Limpieza efectuada.")
        elif text.startswith('http'):
            threading.Thread(target=ejecutar_flujo_descarga, args=(chat_id, text)).start()
            
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}/webhook"), daemon=True).start()
    app.run(host='0.0.0.0', port=puerto)
