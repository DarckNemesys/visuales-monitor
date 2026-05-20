#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import hashlib
import logging
import threading
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin

# ========== CONFIGURACIÓN (LEER VARIABLES DE RENDER) ==========
URL_BASE = os.environ.get("URL_BASE", "https://oops.uclv.edu.cu/")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", 21600))  # 6 horas
ESTADO_FILE = "estado_visuales.json"

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variable global para el scheduler
scheduler_running = True

# ========== FUNCIONES ==========
def enviar_mensaje_telegram(mensaje):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("Faltan TELEGRAM_TOKEN o CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}, timeout=10)
        return r.ok
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        return False

def obtener_contenido_web(url):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f"Error obteniendo {url}: {e}")
        return None

def extraer_items(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href == '../' or href.startswith('?'):
            continue
        full_url = urljoin(base_url, href)
        tipo = 'carpeta' if href.endswith('/') else 'archivo'
        nombre = href.rstrip('/') if href.endswith('/') else href
        items.append({'nombre': nombre, 'tipo': tipo, 'url': full_url})
    return items

def obtener_hash(items):
    return hashlib.md5(json.dumps(items, sort_keys=True).encode()).hexdigest()

def guardar_estado(items, hash_val):
    with open(ESTADO_FILE, 'w') as f:
        json.dump({'timestamp': datetime.now().isoformat(), 'hash': hash_val, 'items': items}, f)

def cargar_estado():
    try:
        with open(ESTADO_FILE, 'r') as f:
            return json.load(f)
    except:
        return None

def monitorear():
    logger.info("Iniciando monitoreo...")
    html = obtener_contenido_web("https://visuales.uclv.cu/")
    if not html:
        html = obtener_contenido_web(URL_BASE)
    if not html:
        logger.error("No se pudo obtener la web")
        return
    items = extraer_items(html, URL_BASE)
    hash_actual = obtener_hash(items)
    estado = cargar_estado()
    if not estado:
        guardar_estado(items, hash_actual)
        enviar_mensaje_telegram("🤖 Monitor iniciado. Vigilando cambios cada 6h.")
        return
    if hash_actual == estado['hash']:
        logger.info("Sin cambios")
        return
    # Hay cambios
    urls_antiguas = {i['url'] for i in estado['items']}
    nuevos = [i for i in items if i['url'] not in urls_antiguas]
    if nuevos:
        msg = f"📢 NUEVO CONTENIDO DETECTADO\n🕐 {datetime.now()}\n"
        for n in nuevos[:10]:
            msg += f"• {n['nombre']} ({n['tipo']})\n"
        if len(nuevos) > 10:
            msg += f"... y {len(nuevos)-10} más"
        enviar_mensaje_telegram(msg)
    guardar_estado(items, hash_actual)

def scheduler_loop():
    global scheduler_running
    logger.info(f"Scheduler iniciado: cada {SCAN_INTERVAL//3600}h")
    while scheduler_running:
        monitorear()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    logger.info("Monitor simple iniciado")
    # Ejecutar una vez al inicio
    monitorear()
    # Iniciar scheduler en segundo plano
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    # Mantener el proceso vivo
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler_running = False
        logger.info("Monitor detenido")