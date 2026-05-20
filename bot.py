#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import hashlib
import zipfile
import logging
import subprocess
import requests
import threading
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

def enviar_documento(chat_id, archivo_path, caption=""):
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

# ========== FUNCIONES DE MONITOREO ==========
def obtener_contenido_web(url):
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Error obteniendo {url}: {e}")
        return None

def extraer_items(html, base_url):
    from urllib.parse import urljoin
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

def monitorear_y_notificar(chat_id=None):
    """Función de monitoreo. Si se provee chat_id, envía resultado al chat."""
    logger.info("Iniciando monitoreo...")
    resultado = {
        'cambios': False,
        'nuevos': [],
        'eliminados': [],
        'mensaje': ''
    }
    
    html = obtener_contenido_web("https://visuales.uclv.cu/")
    if not html:
        html = obtener_contenido_web(URL_BASE)
    if not html:
        logger.error("No se pudo obtener la web")
        if chat_id:
            enviar_mensaje(chat_id, "❌ *Error:* No se pudo conectar con visuales.uclv.cu")
        return resultado
    
    items = extraer_items(html, URL_BASE)
    hash_actual = obtener_hash(items)
    estado = cargar_estado()
    
    if not estado:
        guardar_estado(items, hash_actual)
        if chat_id:
            enviar_mensaje(chat_id, f"📊 *Primer escaneo completado*\n\n📁 Total carpetas: {len([i for i in items if i['tipo'] == 'carpeta'])}\n📄 Total archivos: {len([i for i in items if i['tipo'] == 'archivo'])}")
        resultado['mensaje'] = "Estado inicial guardado"
        return resultado
    
    if hash_actual == estado['hash']:
        if chat_id:
            enviar_mensaje(chat_id, "✅ *Sin cambios*\nNo se detectó nuevo contenido en visuales.uclv.cu")
        resultado['mensaje'] = "Sin cambios"
        return resultado
    
    # Hay cambios
    urls_antiguas = {i['url'] for i in estado['items']}
    urls_actuales = {i['url'] for i in items}
    
    nuevos = [i for i in items if i['url'] not in urls_antiguas]
    eliminados = [i for i in estado['items'] if i['url'] not in urls_actuales]
    
    resultado['cambios'] = True
    resultado['nuevos'] = nuevos
    resultado['eliminados'] = eliminados
    
    if chat_id:
        mensaje = f"📢 *CAMBIOS DETECTADOS* en visuales.uclv.cu\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        if nuevos:
            mensaje += f"🆕 *Nuevos ({len(nuevos)}):*\n"
            for item in nuevos[:10]:
                mensaje += f"• `{item['nombre']}` ({item['tipo']})\n"
            if len(nuevos) > 10:
                mensaje += f"... y {len(nuevos)-10} más\n"
        if eliminados:
            mensaje += f"\n🗑️ *Eliminados ({len(eliminados)}):*\n"
            for item in eliminados[:5]:
                mensaje += f"• `{item['nombre']}`\n"
        mensaje += f"\n💡 Usa `/descargar <url>` para bajar el contenido"
        enviar_mensaje(chat_id, mensaje)
    
    guardar_estado(items, hash_actual)
    resultado['mensaje'] = f"{len(nuevos)} nuevos, {len(eliminados)} eliminados"
    return resultado

# ========== FUNCIONES DE DESCARGA ==========
def descargar_archivo(url, destino):
    nombre = os.path.basename(url)
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    ruta = os.path.join(destino, nombre)
    
    logger.info(f"Descargando {url}")
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    
    with open(ruta, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return ruta

def dividir_archivo(archivo_path):
    parte_size = TAMANO_PARTE_MB * 1024 * 1024
    base = os.path.basename(archivo_path)
    patron = os.path.join(PARTES_DIR, f"{base}.part")
    
    logger.info(f"Dividiendo {archivo_path}")
    subprocess.run(f"split -b {parte_size} '{archivo_path}' '{patron}'", shell=True, check=True)
    
    partes = sorted([os.path.join(PARTES_DIR, f) for f in os.listdir(PARTES_DIR) 
                     if f.startswith(base + ".part")])
    return partes

def limpiar_temporales():
    for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
        for f in os.listdir(dir_path):
            try:
                os.remove(os.path.join(dir_path, f))
            except:
                pass
    logger.info("Archivos temporales limpiados")

def procesar_descarga(chat_id, url):
    try:
        enviar_mensaje(chat_id, f"🔄 *Procesando:* `{url[:80]}...`")
        
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
            enviar_mensaje(chat_id, f"✂️ Archivo de {tamaño/(1024**3):.2f}GB, dividiendo...")
            partes = dividir_archivo(archivo)
            for i, parte in enumerate(partes, 1):
                enviar_mensaje(chat_id, f"📤 Subiendo parte {i}/{len(partes)}")
                enviar_documento(chat_id, parte, f"📦 {os.path.basename(archivo)} - Parte {i}/{len(partes)}")
            enviar_mensaje(chat_id, f"✅ *Descarga completada*")
        
    except Exception as e:
        logger.error(f"Error en descarga: {e}")
        enviar_mensaje(chat_id, f"❌ *Error:* `{str(e)[:150]}`")
    finally:
        limpiar_temporales()

# ========== SERVIDOR FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot activo", "version": "1.0"}

@app.route('/health')
def health():
    return {"status": "healthy"}

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = request.get_json()
        logger.info(f"Webhook recibido")
        
        if 'message' in update:
            msg = update['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip()
            
            if not text:
                return jsonify({"status": "ok"})
            
            logger.info(f"Comando: {text} de {chat_id}")
            
            # ========== COMANDOS ==========
            if text == '/start':
                enviar_mensaje(chat_id, 
                    "🤖 *Bot de Visuales UCLV*\n\n"
                    "📌 *Comandos:*\n"
                    "`/descargar <url>` - Descargar archivo\n"
                    "`/monitorear` - Escanear web manualmente\n"
                    "`/estado` - Ver estado del bot\n"
                    "`/limpiar` - Limpiar temporales\n"
                    "`/ayuda` - Mostrar ayuda")
            
            elif text.startswith('/descargar'):
                partes = text.split(maxsplit=1)
                if len(partes) == 2:
                    thread = threading.Thread(target=procesar_descarga, args=(chat_id, partes[1]))
                    thread.start()
                    enviar_mensaje(chat_id, "🔄 *Descarga iniciada en segundo plano...*")
                else:
                    enviar_mensaje(chat_id, "❌ *Uso:* `/descargar <url>`")
            
            elif text == '/monitorear':
                enviar_mensaje(chat_id, "🔍 *Escaneando visuales.uclv.cu...*\nEsto puede tomar unos segundos.")
                thread = threading.Thread(target=monitorear_y_notificar, args=(chat_id,))
                thread.start()
            
            elif text == '/estado':
                uso = 0
                archivos = 0
                for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
                    for f in os.listdir(dir_path):
                        fp = os.path.join(dir_path, f)
                        if os.path.isfile(fp):
                            uso += os.path.getsize(fp)
                            archivos += 1
                
                estado = cargar_estado()
                ultimo_escaneo = estado.get('timestamp', 'Nunca') if estado else 'Nunca'
                
                enviar_mensaje(chat_id,
                    f"📊 *Estado del bot*\n\n"
                    f"✅ Activo\n"
                    f"💾 Espacio usado: {uso/(1024**3):.2f} GB\n"
                    f"📁 Archivos temp: {archivos}\n"
                    f"🔍 Último escaneo: {ultimo_escaneo[:16] if ultimo_escaneo != 'Nunca' else 'Nunca'}\n"
                    f"📦 Límite: 2GB (dividido en {TAMANO_PARTE_MB}MB)")
            
            elif text == '/limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
            
            elif text == '/ayuda':
                enviar_mensaje(chat_id,
                    "📖 *Ayuda del Bot*\n\n"
                    "🔹 `/descargar <url>` - Descarga un archivo\n"
                    "🔹 `/monitorear` - Escanea la web por cambios\n"
                    "🔹 `/estado` - Ver estado del bot\n"
                    "🔹 `/limpiar` - Limpiar archivos temporales\n"
                    "🔹 `/ayuda` - Mostrar ayuda\n\n"
                    "⚙️ *Comportamiento:*\n"
                    "• Archivos <2GB → envío directo\n"
                    "• Archivos >2GB → divididos en partes de 1.9GB")
            
            else:
                enviar_mensaje(chat_id, f"❌ *Comando no reconocido:* `{text}`\nUsa `/ayuda`")
        
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