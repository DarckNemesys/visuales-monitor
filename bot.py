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

# URLs correctas
URL_VISUALES = "https://visuales.uclv.cu/"
URL_OOPS = "https://oops.uclv.edu.cu/"

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

def formatear_tamaño(tamaño_bytes):
    if tamaño_bytes < 1024:
        return f"{tamaño_bytes} B"
    elif tamaño_bytes < 1024 * 1024:
        return f"{tamaño_bytes/1024:.1f} KB"
    elif tamaño_bytes < 1024 * 1024 * 1024:
        return f"{tamaño_bytes/(1024*1024):.1f} MB"
    else:
        return f"{tamaño_bytes/(1024*1024*1024):.2f} GB"

# ========== FUNCIONES DE SCRAPING CORREGIDAS ==========
def obtener_contenido_web(url):
    """Obtiene el contenido HTML de una URL"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, timeout=30, headers=headers)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Error obteniendo {url}: {e}")
        return None

def extraer_items_directorio(html, base_url):
    """Extrae archivos y carpetas de un listado de directorio Apache"""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    
    # Buscar todos los enlaces en la página
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href == '../' or href.startswith('?'):
            continue
        
        # Construir URL completa
        full_url = urljoin(base_url, href)
        
        # Determinar tipo
        if href.endswith('/'):
            tipo = '📁 Carpeta'
            nombre = href.rstrip('/')
        else:
            tipo = '📄 Archivo'
            nombre = href
        
        items.append({
            'nombre': nombre,
            'tipo': tipo,
            'url': full_url
        })
    
    # Ordenar: carpetas primero, luego archivos
    items.sort(key=lambda x: (x['tipo'] == '📄 Archivo', x['nombre'].lower()))
    
    return items

def explorar_carpeta(base_url, path=""):
    """Explora recursivamente una carpeta y devuelve todos los archivos"""
    from urllib.parse import urljoin
    todos_items = []
    
    url_actual = urljoin(base_url, path)
    logger.info(f"Explorando: {url_actual}")
    
    html = obtener_contenido_web(url_actual)
    if not html:
        return todos_items
    
    soup = BeautifulSoup(html, 'html.parser')
    
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href == '../' or href.startswith('?'):
            continue
        
        full_url = urljoin(url_actual, href)
        
        if href.endswith('/'):
            # Es carpeta, explorar recursivamente
            sub_items = explorar_carpeta(base_url, path + href)
            todos_items.extend(sub_items)
        else:
            # Es archivo
            nombre = href
            todos_items.append({
                'nombre': nombre,
                'tipo': '📄 Archivo',
                'url': full_url
            })
    
    return todos_items

def monitorear_y_notificar(chat_id=None):
    """Monitorea visuales.uclv.cu y envía resultados"""
    logger.info("Iniciando monitoreo...")
    
    if chat_id:
        enviar_mensaje(chat_id, "🔍 *Escaneando visuales.uclv.cu...*\nEsto puede tomar varios segundos...")
    
    # Primero obtener el HTML principal
    html = obtener_contenido_web(URL_VISUALES)
    if not html:
        html = obtener_contenido_web(URL_OOPS)
    
    if not html:
        if chat_id:
            enviar_mensaje(chat_id, "❌ *Error:* No se pudo conectar con la web")
        return None
    
    # Extraer items del directorio raíz
    items_raiz = extraer_items_directorio(html, URL_OOPS)
    
    # También explorar carpetas principales para obtener más archivos
    todos_items = []
    for item in items_raiz:
        todos_items.append(item)
        if item['tipo'] == '📁 Carpeta':
            # Explorar subcarpetas
            sub_items = explorar_carpeta(URL_OOPS, item['nombre'] + '/')
            todos_items.extend(sub_items)
    
    # Eliminar duplicados por URL
    urls_vistas = set()
    items_unicos = []
    for item in todos_items:
        if item['url'] not in urls_vistas:
            urls_vistas.add(item['url'])
            items_unicos.append(item)
    
    hash_actual = hashlib.md5(json.dumps(items_unicos, sort_keys=True).encode()).hexdigest()
    estado = cargar_estado()
    
    carpetas = [i for i in items_unicos if i['tipo'] == '📁 Carpeta']
    archivos = [i for i in items_unicos if i['tipo'] == '📄 Archivo']
    
    if not estado:
        # Primera vez: guardar y mostrar TODO
        guardar_estado(items_unicos, hash_actual)
        
        if chat_id:
            mensaje = f"📊 *PRIMER ESCANEO - CONTENIDO DE LA WEB*\n\n"
            mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
            mensaje += f"🔗 Fuente: `{URL_VISUALES}`\n\n"
            mensaje += f"📁 *Carpetas encontradas: {len(carpetas)}*\n"
            for c in carpetas[:15]:
                mensaje += f"• `{c['nombre']}`\n"
            if len(carpetas) > 15:
                mensaje += f"• ... y {len(carpetas)-15} más\n"
            mensaje += f"\n📄 *Archivos encontrados: {len(archivos)}*\n"
            for a in archivos[:20]:
                mensaje += f"• `{a['nombre']}`\n"
            if len(archivos) > 20:
                mensaje += f"• ... y {len(archivos)-20} más\n"
            mensaje += f"\n✅ Estado guardado. El bot ahora detectará cambios futuros."
            enviar_mensaje(chat_id, mensaje)
        
        return items_unicos
    
    # Verificar cambios
    if hash_actual == estado['hash']:
        if chat_id:
            enviar_mensaje(chat_id, f"✅ *Sin cambios detectados*\n\n📁 Carpetas: {len(carpetas)}\n📄 Archivos: {len(archivos)}")
        return items_unicos
    
    # Hay cambios
    urls_antiguas = {i['url'] for i in estado['items']}
    nuevos = [i for i in items_unicos if i['url'] not in urls_antiguas]
    
    if chat_id:
        mensaje = f"📢 *NUEVO CONTENIDO DETECTADO*\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        if nuevos:
            mensaje += f"🆕 *Nuevos items ({len(nuevos)}):*\n"
            for item in nuevos[:20]:
                mensaje += f"• `{item['nombre']}` ({item['tipo']})\n"
            if len(nuevos) > 20:
                mensaje += f"... y {len(nuevos)-20} más\n"
        mensaje += f"\n💡 Usa `/descargar <url>` para descargar"
        enviar_mensaje(chat_id, mensaje)
    
    guardar_estado(items_unicos, hash_actual)
    return items_unicos

def guardar_estado(items, hash_val):
    with open(ESTADO_FILE, 'w') as f:
        json.dump({'timestamp': datetime.now().isoformat(), 'hash': hash_val, 'items': items}, f)

def cargar_estado():
    try:
        with open(ESTADO_FILE, 'r') as f:
            return json.load(f)
    except:
        return None

# ========== FUNCIONES DE DESCARGA CORREGIDAS ==========
def descargar_archivo(url, destino, chat_id=None):
    """Descarga un archivo desde URL correcta"""
    # Limpiar URL
    url_limpia = url.strip()
    
    # Si la URL es de visuales.uclv.cu, redirige automáticamente
    if 'visuales.uclv.cu' in url_limpia and 'oops' not in url_limpia:
        # Hacer una petición inicial para obtener la redirección
        try:
            response = requests.get(url_limpia, allow_redirects=True, timeout=10)
            url_limpia = response.url
            logger.info(f"Redirigido a: {url_limpia}")
        except Exception as e:
            logger.error(f"Error siguiendo redirección: {e}")
    
    # Extraer nombre del archivo
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
        if 'text/html' in content_type:
            # Puede ser que necesite seguir otra redirección
            if 'oops' not in url_limpia:
                raise Exception("La URL no apunta a un archivo válido. Asegúrate de usar la URL completa del archivo (debe terminar en .mp4, .mkv, .pdf, etc.)")
        
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
        
        if tamaño < 10240:  # Menos de 10KB = algo salió mal
            enviar_mensaje(chat_id, f"❌ *Error:* El archivo descargado es muy pequeño ({formatear_tamaño(tamaño)}).\nLa URL puede ser incorrecta. Asegúrate de que la URL termine con la extensión del archivo (ej: .mp4, .mkv, .pdf).")
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
                    "📌 *Instrucciones para descargar:*\n"
                    "1. Abre la web: visuales.uclv.cu\n"
                    "2. Navega hasta el archivo\n"
                    "3. Copia la URL completa\n"
                    "4. Usa `/descargar <url>`\n\n"
                    "📌 *Ejemplo de URL válida:*\n"
                    "`https://oops.uclv.edu.cu/Peliculas/video.mp4`\n\n"
                    "Usa los botones de abajo:",
                    comandos)
            
            elif text == '📥 Descargar' or text.startswith('/descargar'):
                if text == '📥 Descargar':
                    enviar_mensaje(chat_id, "📥 *Descargar archivo*\n\nEnvía la URL completa del archivo:\n`/descargar https://oops.uclv.edu.cu/ruta/archivo.mp4`\n\n⚠️ La URL debe terminar en .mp4, .mkv, .pdf, etc.")
                else:
                    partes = text.split(maxsplit=1)
                    if len(partes) == 2:
                        thread = threading.Thread(target=procesar_descarga, args=(chat_id, partes[1]))
                        thread.start()
                        enviar_mensaje(chat_id, "🔄 *Descarga iniciada en segundo plano...*")
                    else:
                        enviar_mensaje(chat_id, "❌ *Uso:* `/descargar <url_completa>`")
            
            elif text == '🔍 Monitorear' or text == '/monitorear':
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
                    "🔹 **Para descargar:**\n"
                    "1. Ve a visuales.uclv.cu\n"
                    "2. Encuentra el archivo que quieres\n"
                    "3. Copia la URL completa\n"
                    "4. Usa `/descargar <url>`\n\n"
                    "📌 *Ejemplo de URL correcta:*\n"
                    "`https://oops.uclv.edu.cu/Peliculas/video.mp4`\n\n"
                    "🔹 **Botones:**\n"
                    "• `📥 Descargar` - Descarga un archivo\n"
                    "• `🔍 Monitorear` - Escanea la web\n"
                    "• `📊 Estado` - Ver estado\n"
                    "• `🧹 Limpiar` - Limpia temporales\n\n"
                    "⚙️ *Comportamiento:*\n"
                    "• Archivos <2GB → envío directo\n"
                    "• Archivos >2GB → divididos en partes de 1.9GB")
            
            else:
                enviar_mensaje(chat_id, f"❌ *Comando no reconocido:* `{text[:30]}`\nUsa `/ayuda` o los botones.")
        
        elif 'callback_query' in update:
            callback = update['callback_query']
            callback_id = callback['id']
            chat_id = callback['message']['chat']['id']
            data = callback['data']
            
            if data == 'descargar':
                responder_callback(callback_id, "Envía /descargar <url>")
                enviar_mensaje(chat_id, "📥 Envía:\n`/descargar <url_completa>`")
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