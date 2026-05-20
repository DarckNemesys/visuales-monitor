#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import zipfile
import requests
import threading
from datetime import datetime
from flask import Flask, request, jsonify

# ========== CONFIGURACIÓN ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
URL_BASE = os.environ.get("URL_BASE", "https://oops.uclv.edu.cu/")
LIMITE_2GB = 2 * 1024 * 1024 * 1024

# Directorios temporales
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
COMPRIMIDOS_DIR = os.path.join(BASE_DIR, "comprimidos")
PARTES_DIR = os.path.join(BASE_DIR, "partes")

for d in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

# ========== FUNCIONES DEL BOT ==========
def enviar_mensaje(chat_id, texto):
    """Envía un mensaje a Telegram"""
    url = f"{BOT_URL}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje: {e}")

def enviar_documento(chat_id, archivo_path, caption=""):
    """Envía un documento a Telegram"""
    url = f"{BOT_URL}/sendDocument"
    try:
        with open(archivo_path, 'rb') as f:
            requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"document": f}, timeout=60)
        return True
    except Exception as e:
        print(f"Error enviando documento: {e}")
        return False

def descargar_archivo(url, destino):
    """Descarga un archivo desde URL"""
    nombre = os.path.basename(url)
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    ruta = os.path.join(destino, nombre)
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    with open(ruta, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return ruta

def dividir_archivo(archivo_path, tamaño_parte_mb=1900):
    """Divide un archivo en partes (comando split de Linux)"""
    import subprocess
    parte_size = tamaño_parte_mb * 1024 * 1024
    base = os.path.basename(archivo_path)
    patron = os.path.join(PARTES_DIR, f"{base}.part")
    subprocess.run(f"split -b {parte_size} '{archivo_path}' '{patron}'", shell=True, check=True)
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) if f.startswith(base + ".part")])
    return partes

def procesar_descarga(chat_id, url):
    """Procesa una solicitud de descarga"""
    try:
        enviar_mensaje(chat_id, f"🔄 *Procesando:* `{url[:80]}...`")
        
        # Descargar
        enviar_mensaje(chat_id, "📥 Descargando archivo...")
        archivo = descargar_archivo(url, DESCARGAS_DIR)
        tamaño = os.path.getsize(archivo)
        
        if tamaño <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo archivo... ({tamaño/(1024**2):.1f}MB)")
            enviar_documento(chat_id, archivo, f"✅ {os.path.basename(archivo)}")
        else:
            enviar_mensaje(chat_id, f"✂️ Archivo de {tamaño/(1024**3):.2f}GB, dividiendo...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Subiendo parte {i}/{len(partes)}")
                enviar_documento(chat_id, parte, f"📦 {os.path.basename(archivo)} - Parte {i}")
        
        enviar_mensaje(chat_id, "✅ *Descarga completada*")
        
    except Exception as e:
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
    finally:
        # Limpiar archivos temporales
        for d in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except:
                    pass

# ========== SERVIDOR FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot activo", "version": "1.0"}

@app.route('/health')
def health():
    return {"status": "healthy"}

@app.route(f'/webhook', methods=['POST'])
def webhook():
    """Recibe mensajes de Telegram"""
    try:
        update = request.get_json()
        if 'message' in update:
            msg = update['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '')
            
            # Comandos
            if text.startswith('/start'):
                enviar_mensaje(chat_id, "🤖 *Bot activo*\n\nEnvía /descargar <url>")
            elif text.startswith('/descargar'):
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    # Ejecutar en hilo separado para no bloquear
                    thread = threading.Thread(target=procesar_descarga, args=(chat_id, parts[1]))
                    thread.start()
                    enviar_mensaje(chat_id, "🔄 *Descarga iniciada en segundo plano...*")
                else:
                    enviar_mensaje(chat_id, "❌ Uso: `/descargar <url>`")
            elif text == '/help' or text == '/ayuda':
                enviar_mensaje(chat_id, "📖 *Comandos:*\n/descargar <url> - Descargar archivo\n/start - Iniciar bot\n/estado - Ver estado")
            elif text == '/estado':
                enviar_mensaje(chat_id, "✅ Bot activo\n💾 Espacio: 1GB temporal")
            else:
                enviar_mensaje(chat_id, "Comando no reconocido. Usa /ayuda")
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"Error en webhook: {e}")
        return jsonify({"status": "error"}), 500

# ========== CONFIGURAR WEBHOOK ==========
def set_webhook():
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_TOKEN no configurado")
        return
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL", os.environ.get("WEBHOOK_URL"))
    if not webhook_url:
        print("ERROR: WEBHOOK_URL no configurado")
        return
    url = f"{BOT_URL}/setWebhook?url={webhook_url}/webhook"
    response = requests.get(url)
    print(f"Webhook configurado: {response.json()}")

# ========== MAIN ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    
    # Configurar webhook al iniciar
    time.sleep(2)  # Pequeña pausa para que el servidor esté listo
    set_webhook()
    
    print(f"Iniciando bot en puerto {port}")
    app.run(host='0.0.0.0', port=port)