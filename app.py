#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Monitor Sincronizado de Visuales UCLV
Rastrea de forma controlada la aparición de nuevos archivos en el repositorio.
"""

from flask import Flask
import threading
import time
import os
import requests
from bs4 import BeautifulSoup
import hashlib
import json
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urljoin, urlparse
from typing import List, Optional

load_dotenv()
app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
URL_BASE = os.environ.get("URL_BASE", "https://oops.uclv.edu.cu/")

if not URL_BASE.endswith("/"):
    URL_BASE += "/"

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", 21600))  # Cada 6 horas
MAX_DEPTH = 10
MAX_RETRIES = 3
TIMEOUT = 60
ESTADO_FILE = "estado_visuales.json"

def enviar_alerta_telegram(mensaje: str):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: 
        requests.post(url, json={"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e: 
        print(f"Error enviando alerta monitor: {e}")

def fetch_html_con_reintentos(url: str, retries: int = MAX_RETRIES) -> Optional[str]:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for intento in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code == 200: return r.text
        except Exception: 
            time.sleep(3)
    return None

def mapear_servidor_recursivo(url_actual: str, profundidad_actual: int = 0) -> List[str]:
    if profundidad_actual > MAX_DEPTH: return []
    lista_urls = []
    html_content = fetch_html_con_reintentos(url_actual)
    if not html_content: return []

    soup = BeautifulSoup(html_content, 'html.parser')
    for tag in soup.find_all('a'):
        href = tag.get('href')
        if not href or href in ['../', './'] or href.startswith('?'): continue
        
        url_completa = urljoin(url_actual, href)
        if urlparse(url_completa).netloc != urlparse(URL_BASE).netloc: continue
        
        lista_urls.append(url_completa)
        if href.endswith('/'):
            lista_urls.extend(mapear_servidor_recursivo(url_completa, profundidad_actual + 1))
    return lista_urls

def ejecutar_monitoreo():
    try:
        estado_previo = {}
        if os.path.exists(ESTADO_FILE):
            try:
                with open(ESTADO_FILE, 'r') as f: estado_previo = json.load(f)
            except Exception: pass

        urls_encontradas = sorted(list(set(mapear_servidor_recursivo(URL_BASE))))
        if not urls_encontradas: return

        cadena_control = "".join(urls_encontradas).encode('utf-8')
        hash_actual = hashlib.md5(cadena_control).hexdigest()

        if estado_previo.get('hash') != hash_actual:
            total_viejas = estado_previo.get('items_count', 0)
            nuevas_entradas = len(urls_encontradas) - total_viejas if len(urls_encontradas) > total_viejas else 0
            
            mensaje = (
                f"📢 *CAMBIOS DETECTADOS EN VISUALES UCLV*\n\n"
                f"🌐 *Servidor origen:* {URL_BASE}\n"
                f"📊 *Total Elementos Indexados:* {len(urls_encontradas)} URLs\n"
                f"➕ *Nuevos archivos detectados:* {nuevas_entradas}\n"
            )
            enviar_alerta_telegram(mensaje)
            with open(ESTADO_FILE, 'w') as f:
                json.dump({'hash': hash_actual, 'timestamp': datetime.now().isoformat(), 'items_count': len(urls_encontradas)}, f, indent=2)
    except Exception as e: 
        print(f"Error en ejecución de monitor: {e}")

def daemon_scheduler():
    while True:
        ejecutar_monitoreo()
        time.sleep(SCAN_INTERVAL)

@app.route('/')
def home(): return "Monitor Run Activo", 200

@app.route('/health')
def health(): return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=daemon_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
