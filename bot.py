#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import hashlib
import logging
import subprocess
import requests
import urllib.parse
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

def enviar_mensaje_con_teclado(chat_id, texto, comandos):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    keyboard = [[{"text": cmd}] for cmd in comandos]
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown",
        "reply_markup": {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": False}
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

def responder_callback(callback_id, texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        payload = {"callback_query_id": callback_id, "text": texto, "show_alert": False}
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        logger.error(f"Error respondiendo callback: {e}")
        return False

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
        tipo = '📁 Carpeta' if href.endswith('/') else '📄 Archivo'
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

def formatear_tamaño(tamaño_bytes):
    if tamaño_bytes < 1024:
        return f"{tamaño_bytes} B"
    elif tamaño_bytes < 1024 * 1024:
        return f"{tamaño_bytes/1024:.1f} KB"
    elif tamaño_bytes < 1024 * 1024 * 1024:
        return f"{tamaño_bytes/(1024*1024):.1f} MB"
    else:
        return f"{tamaño_bytes/(1024*1024*1024):.2f} GB"

def monitorear_y_notificar(chat_id=None):
    """Función de monitoreo. Si se provee chat_id, envía resultado al chat."""
    logger.info("Iniciando monitoreo...")
    
    html = obtener_contenido_web("https://visuales.uclv.cu/")
    if not html:
        html = obtener_contenido_web(URL_BASE)
    if not html:
        if chat_id:
            enviar_mensaje(chat_id, "❌ *Error:* No se pudo conectar con visuales.uclv.cu\n\nLa web puede estar caída o la URL es incorrecta.")
        return None
    
    items = extraer_items(html, URL_BASE)
    hash_actual = obtener_hash(items)
    estado = cargar_estado()
    
    # Contar carpetas y archivos
    carpetas = [i for i in items if i['tipo'] == '📁 Carpeta']
    archivos = [i for i in items if i['tipo'] == '📄 Archivo']
    
    if not estado:
        # PRIMERA VEZ: Guardar y mostrar TODO el contenido
        guardar_estado(items, hash_actual)
        
        if chat_id:
            # Mostrar resumen completo
            mensaje = f"📊 *PRIMER ESCANEO - CONTENIDO ACTUAL*\n\n"
            mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
            mensaje += f"🔗 Fuente: `{URL_BASE}`\n\n"
            mensaje += f"📁 *Carpetas ({len(carpetas)}):*\n"
            for c in carpetas[:20]:
                mensaje += f"• `{c['nombre']}`\n"
            if len(carpetas) > 20:
                mensaje += f"• ... y {len(carpetas)-20} más\n"
            mensaje += f"\n📄 *Archivos ({len(archivos)}):*\n"
            for a in archivos[:30]:
                mensaje += f"• `{a['nombre']}`\n"
            if len(archivos) > 30:
                mensaje += f"• ... y {len(archivos)-30} más\n"
            mensaje += f"\n✅ Estado guardado. El bot ahora monitoreará cambios futuros."
            enviar_mensaje(chat_id, mensaje)
        return items
    
    if hash_actual == estado['hash']:
        if chat_id:
            enviar_mensaje(chat_id, f"✅ *Sin cambios*\n\n📁 Carpetas: {len(carpetas)}\n📄 Archivos: {len(archivos)}\n🕐 Último cambio: {estado.get('timestamp', 'Desconocido')[:16]}")
        return items
    
    # Hay cambios: mostrar solo los nuevos
    urls_antiguas = {i['url'] for i in estado['items']}
    nuevos = [i for i in items if i['url'] not in urls_antiguas]
    eliminados = [i for i in estado['items'] if i['url'] not in urls_actuales]
    
    if chat_id:
        mensaje = f"📢 *CAMBIOS DETECTADOS*\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        if nuevos:
            mensaje += f"🆕 *Nuevos ({len(nuevos)}):*\n"
            for item in nuevos[:15]:
                mensaje += f"• `{item['nombre']}` ({item['tipo']})\n"
            if len(nuevos) > 15:
                mensaje += f"... y {len(nuevos)-15} más\n"
        if eliminados:
            mensaje += f"\n🗑️ *Eliminados ({len(eliminados)}):*\n"
            for item in eliminados[:10]:
                mensaje += f"• `{item['nombre']}`\n"
        mensaje += f"\n💡 Usa `/descargar <url>` para bajar el contenido"
        enviar_mensaje(chat_id, mensaje)
    
    guardar_estado(items, hash_actual)
    return items

# ========== FUNCIONES DE DESCARGA ==========
def descargar_archivo(url, destino, chat_id=None):
    """Descarga un archivo desde URL con manejo correcto de URLs codificadas"""
    # Decodificar y limpiar URL
    url_limpia = url.strip()
    
    # Extraer nombre del archivo de la URL
    nombre = os.path.basename(urllib.parse.unquote(url_limpia))
    if not nombre or '.' not in nombre:
        nombre = f"descarga_{int(time.time())}"
    
    ruta = os.path.join(destino, nombre)
    
    logger.info(f"Descargando: {url_limpia}")
    if chat_id:
        enviar_mensaje(chat_id, f"📥 Descargando: `{nombre[:50]}`")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url_limpia, stream=True, timeout=120, headers=headers)
        response.raise_for_status()
        
        # Verificar que no es HTML
        content_type = response.headers.get('content-type', '')
        if 'text/html' in content_type and 'video' not in content_type:
            raise Exception("La URL devolvió HTML en lugar de un archivo. Verifica que la URL sea correcta.")
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        
        with open(ruta, 'wb') as f:
            for chunk in response.iter_content(chunk_size=32768):
                f.write(chunk)
                downloaded += len(chunk)
                if chat_id and total_size > 0:
                    percent = (downloaded / total_size) * 100
                    if int(percent) % 20 == 0 and percent > 0:
                        enviar_mensaje(chat_id, f"📥 Progreso: {percent:.0f}% ({formatear_tamaño(downloaded)})")
        
        return ruta
    
    except Exception as e:
        logger.error(f"Error descargando: {e}")
        raise

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
        enviar_mensaje(chat_id, f"🔄 *Procesando descarga...*")
        
        archivo = descargar_archivo(url, DESCARGAS_DIR, chat_id)
        tamaño = os.path.getsize(archivo)
        
        if tamaño < 1024:  # Menos de 1KB = algo salió mal
            enviar_mensaje(chat_id, f"❌ *Error:* El archivo descargado es muy pequeño ({formatear_tamaño(tamaño)}).\nLa URL puede ser incorrecta o la web está devolviendo HTML.")
            return
        
        if tamaño <= LIMITE_2GB:
            enviar_mensaje(chat_id, f"📤 Subiendo archivo... ({formatear_tamaño(tamaño)})")
            if enviar_documento(chat_id, archivo, f"✅ {os.path.basename(archivo)}"):
                enviar_mensaje(chat_id, "✅ *Descarga completada*")
            else:
                enviar_mensaje(chat_id, "❌ Error al subir el archivo")
        else:
            enviar_mensaje(chat_id, f"✂️ Archivo de {formatear_tamaño(tamaño)}, dividiendo...")
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
                    "✅ Bot funcionando correctamente\n\n"
                    "Usa los botones de abajo:\n"
                    "• `📥 Descargar` - Descarga archivos\n"
                    "• `🔍 Monitorear` - Escanea la web\n"
                    "• `📊 Estado` - Ver estado\n"
                    "• `🧹 Limpiar` - Limpia temporales\n"
                    "• `❓ Ayuda` - Esta ayuda",
                    comandos)
            
            elif text == '📥 Descargar' or text.startswith('/descargar'):
                if text == '📥 Descargar':
                    enviar_mensaje(chat_id, "📥 *Descargar archivo*\n\nEnvía la URL completa del archivo:\n`/descargar https://oops.uclv.edu.cu/ruta/archivo.mp4`")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        thread = threading.Thread(target=procesar_descarga, args=(chat_id, partes[1]))
                        thread.start()
                        enviar_mensaje(chat_id, "🔄 *Descarga iniciada en segundo plano...*")
                    else:
                        enviar_mensaje(chat_id, "❌ *Uso:* `/descargar <url_completa>`")
            
            elif text == '🔍 Monitorear' or text == '/monitorear':
                enviar_mensaje(chat_id, "🔍 *Escaneando visuales.uclv.cu...*\nEsto puede tomar unos segundos.")
                thread = threading.Thread(target=monitorear_y_notificar, args=(chat_id,))
                thread.start()
            
            elif text == '📊 Estado' or text == '/estado':
                uso = 0
                archivos_temp = 0
                for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
                    for f in os.listdir(dir_path):
                        fp = os.path.join(dir_path, f)
                        if os.path.isfile(fp):
                            uso += os.path.getsize(fp)
                            archivos_temp += 1
                
                estado = cargar_estado()
                if estado:
                    ultimo_escaneo = estado.get('timestamp', 'Nunca')[:16]
                    total_items = len(estado.get('items', []))
                else:
                    ultimo_escaneo = "Nunca"
                    total_items = 0
                
                enviar_mensaje(chat_id,
                    f"📊 *Estado del bot*\n\n"
                    f"✅ Activo\n"
                    f"💾 Espacio usado: {formatear_tamaño(uso)}\n"
                    f"📁 Archivos temp: {archivos_temp}\n"
                    f"🔍 Último escaneo: {ultimo_escaneo}\n"
                    f"📋 Items monitoreados: {total_items}\n"
                    f"📦 Límite: 2GB (dividido en {TAMANO_PARTE_MB}MB)")
            
            elif text == '🧹 Limpiar' or text == '/limpiar':
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
            
            elif text == '❓ Ayuda' or text == '/ayuda':
                enviar_mensaje(chat_id,
                    "📖 *Ayuda del Bot*\n\n"
                    "🔹 **Botones:**\n"
                    "• `📥 Descargar` - Descarga un archivo\n"
                    "• `🔍 Monitorear` - Escanea la web\n"
                    "• `📊 Estado` - Ver estado\n"
                    "• `🧹 Limpiar` - Limpia temporales\n\n"
                    "🔹 **Comandos manuales:**\n"
                    "`/descargar <url>`\n`/monitorear`\n`/estado`\n`/limpiar`\n`/ayuda`\n\n"
                    "⚙️ *Comportamiento:*\n"
                    "• Archivos <2GB → envío directo\n"
                    "• Archivos >2GB → divididos en partes de 1.9GB\n\n"
                    "📌 *Ejemplo de URL válida:*\n"
                    "`https://oops.uclv.edu.cu/Peliculas/video.mp4`")
            
            else:
                enviar_mensaje(chat_id, f"❌ *Comando no reconocido:* `{text[:30]}`\nUsa `/ayuda` o los botones.")
        
        elif 'callback_query' in update:
            callback = update['callback_query']
            callback_id = callback['id']
            chat_id = callback['message']['chat']['id']
            data = callback['data']
            
            if data == 'descargar':
                responder_callback(callback_id, "Envía /descargar <url>")
                enviar_mensaje(chat_id, "📥 Envía el comando:\n`/descargar <url_completa>`")
            elif data == 'monitorear':
                responder_callback(callback_id, "Escaneando...")
                thread = threading.Thread(target=monitorear_y_notificar, args=(chat_id,))
                thread.start()
            elif data == 'estado':
                responder_callback(callback_id, "Obteniendo estado...")
                uso = 0
                archivos_temp = 0
                for dir_path in [DESCARGAS_DIR, COMPRIMIDOS_DIR, PARTES_DIR]:
                    for f in os.listdir(dir_path):
                        fp = os.path.join(dir_path, f)
                        if os.path.isfile(fp):
                            uso += os.path.getsize(fp)
                            archivos_temp += 1
                estado = cargar_estado()
                ultimo_escaneo = estado.get('timestamp', 'Nunca')[:16] if estado else 'Nunca'
                enviar_mensaje(chat_id, f"📊 *Estado*\n💾 {formatear_tamaño(uso)}\n📁 {archivos_temp} archivos\n🔍 Último escaneo: {ultimo_escaneo}")
            elif data == 'limpiar':
                responder_callback(callback_id, "Limpiando...")
                limpiar_temporales()
                enviar_mensaje(chat_id, "🧹 *Archivos temporales limpiados*")
            elif data == 'ayuda':
                responder_callback(callback_id, "Ayuda")
                enviar_mensaje(chat_id, "📖 Usa /ayuda para ver todos los comandos")
        
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