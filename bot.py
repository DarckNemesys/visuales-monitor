#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import zipfile
import logging
import subprocess
import requests
import threading
from datetime import datetime
from flask import Flask, request, jsonify

# ========== CONFIGURACIÓN ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")  # Para envíos automáticos del monitor
URL_BASE = os.environ.get("URL_BASE", "https://oops.uclv.edu.cu/")
LIMITE_2GB = 2 * 1024 * 1024 * 1024
TAMANO_PARTE_MB = 1900

# Directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS_DIR = os.path.join(BASE_DIR, "descargas")
COMPRIMIDOS_DIR = os.path.join(BASE_DIR, "comprimidos")
PARTES_DIR = os.path.join(BASE_DIR, "partes")
ESTADO_FILE = os.path.join(BASE_DIR, "estado_visuales.json")

for d in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
    os.makedirs(d, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== FUNCIONES DE TELEGRAM ==========
def enviar_mensaje(chat_id, texto):
    """Envía un mensaje a Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        logger.error(f"Error enviando mensaje: {e}")
        return False

def enviar_documento(chat_id, archivo_path, caption=""):
    """Envía un documento a Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(archivo_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': chat_id, 'caption': caption[:1024]}
            response = requests.post(url, data=data, files=files, timeout=120)
            return response.ok
    except Exception as e:
        logger.error(f"Error enviando documento: {e}")
        return False

def obtener_actualizaciones(offset=None):
    """Obtiene actualizaciones de Telegram (polling)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    try:
        response = requests.get(url, params=params, timeout=35)
        return response.json().get("result", [])
    except Exception as e:
        logger.error(f"Error obteniendo updates: {e}")
        return []

# ========== FUNCIONES DE DESCARGA ==========
def descargar_archivo(url, destino):
    """Descarga un archivo desde URL con barra de progreso"""
    nombre = os.path.basename(url)
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    ruta = os.path.join(destino, nombre)
    
    logger.info(f"Descargando {url} -> {ruta}")
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    
    total = int(response.headers.get('content-length', 0))
    descargado = 0
    
    with open(ruta, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            descargado += len(chunk)
            if total > 0:
                percent = (descargado / total) * 100
                if int(percent) % 10 == 0:
                    logger.info(f"Progreso: {percent:.1f}%")
    return ruta

def dividir_archivo(archivo_path):
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
    """Limpia todos los directorios temporales"""
    for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            try:
                os.remove(os.path.join(dir_path, f))
            except:
                pass
    logger.info("Archivos temporales limpiados")

# ========== PROCESAMIENTO DE DESCARGA ==========
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
            if enviar_documento(chat_id, archivo, f"✅ {os.path.basename(archivo)}"):
                enviar_mensaje(chat_id, "✅ *Descarga completada*")
            else:
                enviar_mensaje(chat_id, "❌ Error al subir el archivo")
        else:
            enviar_mensaje(chat_id, f"✂️ Archivo de {tamaño/(1024**3):.2f}GB, dividiendo en partes...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Subiendo parte {i}/{len(partes)}")
                enviar_documento(chat_id, parte, f"📦 {os.path.basename(archivo)} - Parte {i}/{len(partes)}")
            enviar_mensaje(chat_id, f"✅ *Descarga completada*\n\n📌 Para unir las partes:\n`cat {os.path.basename(archivo)}.part* > {os.path.basename(archivo)}`")
        
    except Exception as e:
        logger.error(f"Error en descarga: {e}")
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
    finally:
        limpiar_temporales()

# ========== COMANDOS ==========
def procesar_comando(chat_id, text):
    """Procesa los comandos del bot"""
    text = text.strip()
    
    if text == '/start':
        enviar_mensaje(chat_id, 
            "🤖 *Bot de Visuales UCLV*\n\n"
            "📌 *Comandos disponibles:*\n"
            "`/descargar <url>` - Descargar archivo\n"
            "`/monitorear` - Ver último estado de la web\n"
            "`/limpiar` - Limpiar archivos temporales\n"
            "`/estado` - Ver estado del bot\n"
            "`/ayuda` - Mostrar esta ayuda\n\n"
            "💡 *Ejemplo:*\n"
            "`/descargar https://oops.uclv.edu.cu/video.mp4`")
    
    elif text.startswith('/descargar'):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            thread = threading.Thread(target=procesar_descarga, args=(chat_id, parts[1]))
            thread.start()
            enviar_mensaje(chat_id, "🔄 *Descarga iniciada en segundo plano...*")
        else:
            enviar_mensaje(chat_id, "❌ *Uso:* `/descargar <url>`")
    
    elif text == '/estado':
        # Calcular espacio usado
        uso = 0
        archivos = 0
        for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
            for f in os.listdir(dir_path):
                fp = os.path.join(dir_path, f)
                if os.path.isfile(fp):
                    uso += os.path.getsize(fp)
                    archivos += 1
        enviar_mensaje(chat_id,
            f"📊 *Estado del bot*\n\n"
            f"✅ Activo\n"
            f"💾 Espacio usado: {uso/(1024**3):.2f} GB\n"
            f"📁 Archivos temporales: {archivos}\n"
            f"📦 Límite sin dividir: 2GB\n"
            f"✂️ Tamaño de parte: {TAMANO_PARTE_MB}MB")
    
    elif text == '/limpiar':
        limpiar_temporales()
        enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
    
    elif text == '/monitorear':
        enviar_mensaje(chat_id, "📢 *Monitoreo de visuales.uclv.cu*\n\nEjecutando escaneo...")
        # Aquí puedes añadir la función de monitoreo
        enviar_mensaje(chat_id, "🔍 Función en desarrollo. Próximamente.")
    
    elif text == '/ayuda':
        enviar_mensaje(chat_id,
            "📖 *Ayuda del Bot*\n\n"
            "🔹 `/descargar <url>` - Descarga un archivo desde la URL\n"
            "🔹 `/monitorear` - Escanea visuales.uclv.cu en busca de novedades\n"
            "🔹 `/limpiar` - Limpia archivos temporales del servidor\n"
            "🔹 `/estado` - Muestra el estado del bot\n"
            "🔹 `/ayuda` - Muestra esta ayuda\n\n"
            "⚙️ *Comportamiento:*\n"
            "• Archivos <2GB → envío directo\n"
            "• Archivos >2GB → divididos en partes de 1.9GB\n\n"
            "📌 *Ejemplo:*\n"
            "`/descargar https://oops.uclv.edu.cu/video.mp4`")
    
    else:
        enviar_mensaje(chat_id, "❌ *Comando no reconocido.*\nUsa `/ayuda` para ver los comandos disponibles.")

# ========== POLLING ==========
def polling_loop():
    """Bucle principal de polling para recibir mensajes"""
    last_update_id = 0
    logger.info("Iniciando polling...")
    
    while True:
        try:
            updates = obtener_actualizaciones(last_update_id + 1 if last_update_id else None)
            
            for update in updates:
                if 'message' in update:
                    msg = update['message']
                    chat_id = msg['chat']['id']
                    text = msg.get('text', '')
                    
                    if text:
                        logger.info(f"Mensaje de {chat_id}: {text[:50]}")
                        procesar_comando(chat_id, text)
                
                last_update_id = update.get('update_id', last_update_id)
            
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Error en polling: {e}")
            time.sleep(5)

# ========== SERVIDOR FLASK (para mantener vivo el servicio) ==========
app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot activo", "version": "1.0", "comandos": ["/start", "/descargar", "/estado", "/limpiar", "/monitorear", "/ayuda"]}

@app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.route('/ping')
def ping():
    return "pong"

# ========== MAIN ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    
    # Iniciar polling en hilo separado
    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()
    logger.info("Polling iniciado en segundo plano")
    
    # Iniciar servidor Flask
    logger.info(f"Iniciando servidor web en puerto {port}")
    app.run(host='0.0.0.0', port=port)