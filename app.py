#!/usr/bin/env python3
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

load_dotenv()

app = Flask(__name__)

# Configuración
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
URL_BASE = os.environ.get("URL_BASE", "https://visuales.uclv.cu/")
if not URL_BASE.endswith("/"):
    URL_BASE += "/"
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", 21600))

MAX_DEPTH = 15
MAX_RETRIES = 3
TIMEOUT = 120

estado_file = "estado_visuales.json"

def enviar_mensaje(mensaje):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje: {e}")

def fetch_with_retry(url, retries=MAX_RETRIES):
    for attempt in range(retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            r = requests.get(url, timeout=TIMEOUT, headers=headers, allow_redirects=True)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"Intento {attempt + 1}/{retries} fallido para {url}: {e}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    return None

def scrape_page(url, base_domain, visited, depth, max_depth):
    if depth > max_depth:
        return visited
    
    if url in visited:
        return visited
    
    visited.add(url)
    print(f"[{depth}/{max_depth}] Scrapeando: {url}")
    
    html = fetch_with_retry(url)
    if not html:
        return visited
    
    soup = BeautifulSoup(html, 'html.parser')
    
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href:
            continue
        
        full_url = urljoin(url, href)
        parsed = urlparse(full_url)
        
        if parsed.netloc != base_domain:
            continue
        
        if parsed.path in ['', '/', '../'] or parsed.path.startswith('?'):
            continue
        
        if full_url not in visited:
            scrape_page(full_url, base_domain, visited, depth + 1, max_depth)
    
    return visited

def scrape_deep(base_url, max_depth=MAX_DEPTH):
    parsed = urlparse(base_url)
    base_domain = parsed.netloc
    
    visited = set()
    scrape_page(base_url, base_domain, visited, 0, max_depth)
    
    items = []
    for url in visited:
        relative = url.replace(base_url, "", 1) if url.startswith(base_url) else url
        items.append(relative if relative else "/")
    
    return sorted(items)

def monitorear():
    print("Ejecutando monitoreo profundo...")
    try:
        items = scrape_deep(URL_BASE)
        print(f"Total URLs encontradas: {len(items)}")
        
        hash_actual = hashlib.md5(str(items).encode()).hexdigest()
        estado = {}
        try:
            with open(estado_file, 'r') as f:
                estado = json.load(f)
        except:
            pass
        
        if estado.get('hash') != hash_actual:
            nuevos = len(items) - len(estado.get('items_count', 0))
            mensaje = (
                f"📢 *CAMBIOS DETECTADOS*\n\n"
                f"🌐 {URL_BASE}\n"
                f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📊 Total URLs: {len(items)}\n"
                f"➕ Nuevas: {nuevos}\n"
                f"📁 Profundidad: {MAX_DEPTH} capas"
            )
            enviar_mensaje(mensaje)
            with open(estado_file, 'w') as f:
                json.dump({
                    'hash': hash_actual,
                    'timestamp': datetime.now().isoformat(),
                    'items_count': len(items)
                }, f)
            print(f"Cambios detectados. {len(items)} URLs totales.")
        else:
            print("Sin cambios")
    except Exception as e:
        print(f"Error en monitoreo: {e}")
        enviar_mensaje(f"❌ *Error en monitoreo:*\n`{str(e)[:200]}`")

def scheduler():
    while True:
        monitorear()
        time.sleep(SCAN_INTERVAL)

# Endpoint para mantener el servicio vivo
@app.route('/')
def home():
    return "Monitor de Visuales UCLV - Activo"

@app.route('/health')
def health():
    return "OK"

if __name__ == "__main__":
    # Iniciar scheduler en segundo plano
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)