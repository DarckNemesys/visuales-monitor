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

app = Flask(__name__)

# Configuración
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
URL_BASE = "https://oops.uclv.edu.cu/"
SCAN_INTERVAL = 21600  # 6 horas

# Variables del monitor
estado_file = "estado_visuales.json"

def enviar_mensaje(mensaje):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Error: {e}")

def monitorear():
    print("Ejecutando monitoreo...")
    try:
        r = requests.get("https://visuales.uclv.cu/", timeout=30)
        soup = BeautifulSoup(r.text, 'html.parser')
        items = []
        for a in soup.find_all('a'):
            href = a.get('href')
            if href and href not in ['../', '?'] and not href.startswith('?'):
                items.append(href)
        
        hash_actual = hashlib.md5(str(sorted(items)).encode()).hexdigest()
        estado = {}
        try:
            with open(estado_file, 'r') as f:
                estado = json.load(f)
        except:
            pass
        
        if estado.get('hash') != hash_actual:
            enviar_mensaje(f"📢 CAMBIOS DETECTADOS en {URL_BASE}\n🕐 {datetime.now()}")
            with open(estado_file, 'w') as f:
                json.dump({'hash': hash_actual, 'timestamp': datetime.now().isoformat()}, f)
        else:
            print("Sin cambios")
    except Exception as e:
        print(f"Error en monitoreo: {e}")

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