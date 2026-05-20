#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Monitor de visuales.uclv.cu
Detecta nuevos archivos/carpetas y envía alertas a Telegram
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ========== CONFIGURACIÓN ==========
# URL base (visuales redirige a oops)
URL_BASE = "https://oops.uclv.edu.cu/"

# Token y Chat ID del grupo/canal
TELEGRAM_TOKEN = "8723078700:AAGr_-qY3zhbXBRlxS-6aLWR6hQ8_O-fTDY"  # El mismo token del bot descargador
CHAT_ID = "1003629609415"      # ID del grupo donde enviar las alertas

# Archivo para guardar el estado anterior
ESTADO_FILE = os.path.join(os.path.dirname(__file__), "estado_visuales.json")

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ========== FUNCIONES DE SCRAPING ==========

def obtener_contenido_web(url):
    """Obtiene el contenido HTML de la URL"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Error al obtener {url}: {e}")
        return None


def extraer_items_directorio(html, base_url):
    """
    Extrae la lista de archivos y carpetas de un listado de directorio Apache
    Devuelve una lista de diccionarios con nombre, tipo, tamaño y url
    """
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    
    # Buscar todas las filas de la tabla de archivos
    # Los directorios Apache típicamente tienen <tr> con <a> para los enlaces
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href == '../' or href.startswith('?'):
            continue
        
        # Construir URL completa
        full_url = urljoin(base_url, href)
        
        # Determinar si es carpeta o archivo
        if href.endswith('/'):
            tipo = 'carpeta'
            nombre = href.rstrip('/')
        else:
            tipo = 'archivo'
            nombre = href
        
        # Buscar tamaño y fecha si están disponibles (en la misma fila)
        size = None
        date = None
        
        # Buscar la fila padre que contiene este enlace
        parent_row = a.find_parent('tr')
        if parent_row:
            celdas = parent_row.find_all('td')
            if len(celdas) >= 3:
                # En los listados Apache, el formato es: [nombre] [fecha] [tamaño]
                if celdas[1].text.strip():
                    date = celdas[1].text.strip()
                if len(celdas) > 2 and celdas[2].text.strip():
                    size = celdas[2].text.strip()
        
        items.append({
            'nombre': nombre,
            'tipo': tipo,
            'url': full_url,
            'fecha': date,
            'tamaño': size
        })
    
    return items


def obtener_hash_contenido(items):
    """Calcula un hash único del contenido para detectar cambios"""
    # Ordenar por nombre para consistencia
    items_str = json.dumps(items, sort_keys=True)
    return hashlib.md5(items_str.encode()).hexdigest()


def obtener_estado_guardado():
    """Lee el estado guardado del archivo JSON"""
    if os.path.exists(ESTADO_FILE):
        try:
            with open(ESTADO_FILE, 'r') as f:
                return json.load(f)
        except:
            return None
    return None


def guardar_estado(items, hash_content):
    """Guarda el estado actual en el archivo JSON"""
    estado = {
        'timestamp': datetime.now().isoformat(),
        'hash': hash_content,
        'items': items
    }
    with open(ESTADO_FILE, 'w') as f:
        json.dump(estado, f, indent=2)
    logger.info(f"Estado guardado con {len(items)} items")


# ========== FUNCIONES DE TELEGRAM ==========

def enviar_mensaje_telegram(mensaje):
    """Envía un mensaje al grupo/canal de Telegram"""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "AQUI_TU_TOKEN_DEL_BOT":
        logger.warning("Token de Telegram no configurado")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Mensaje enviado a Telegram")
    except Exception as e:
        logger.error(f"Error al enviar mensaje: {e}")


def enviar_alerta_nuevos_items(nuevos_items):
    """Envía una alerta formateada con los nuevos items encontrados"""
    if not nuevos_items:
        return
    
    # Separar carpetas y archivos
    nuevas_carpetas = [i for i in nuevos_items if i['tipo'] == 'carpeta']
    nuevos_archivos = [i for i in nuevos_items if i['tipo'] == 'archivo']
    
    # Construir mensaje
    mensaje = f"📢 *NUEVO CONTENIDO DETECTADO*\n\n"
    mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    mensaje += f"🔗 Fuente: `{URL_BASE}`\n\n"
    
    if nuevas_carpetas:
        mensaje += f"📁 *Nuevas carpetas ({len(nuevas_carpetas)}):*\n"
        for carpeta in nuevas_carpetas[:10]:  # Limitar a 10
            mensaje += f"• `{carpeta['nombre']}`\n"
        if len(nuevas_carpetas) > 10:
            mensaje += f"• ... y {len(nuevas_carpetas) - 10} más\n"
        mensaje += "\n"
    
    if nuevos_archivos:
        mensaje += f"📄 *Nuevos archivos ({len(nuevos_archivos)}):*\n"
        for archivo in nuevos_archivos[:15]:  # Limitar a 15
            tamaño = f" ({archivo['tamaño']})" if archivo['tamaño'] else ""
            mensaje += f"• `{archivo['nombre']}`{tamaño}\n"
        if len(nuevos_archivos) > 15:
            mensaje += f"• ... y {len(nuevos_archivos) - 15} más\n"
        mensaje += "\n"
    
    mensaje += f"💡 *Para descargar*, usa el comando:\n"
    mensaje += f"`/descargar <url_completa>`"
    
    enviar_mensaje_telegram(mensaje)


def enviar_resumen_inicial(items):
    """Envía un resumen completo del contenido actual (primera ejecución)"""
    carpetas = [i for i in items if i['tipo'] == 'carpeta']
    archivos = [i for i in items if i['tipo'] == 'archivo']
    
    mensaje = f"🤖 *Monitor de Visuales UCLV ACTIVADO*\n\n"
    mensaje += f"📊 *Estado inicial del repositorio:*\n"
    mensaje += f"📁 Carpetas: {len(carpetas)}\n"
    mensaje += f"📄 Archivos: {len(archivos)}\n"
    mensaje += f"🔗 URL base: `{URL_BASE}`\n\n"
    mensaje += f"⚠️ *Importante:* Este bot monitoreará la web y te avisará cuando haya nuevo contenido.\n\n"
    mensaje += f"📌 *Para descargar cualquier archivo*, usa el comando `/descargar <url>` en el grupo del bot descargador."
    
    enviar_mensaje_telegram(mensaje)


# ========== FUNCIÓN PRINCIPAL ==========

def comparar_y_notificar(items_actuales, hash_actual):
    """Compara el estado actual con el guardado y notifica cambios"""
    estado_guardado = obtener_estado_guardado()
    
    if estado_guardado is None:
        # Primera ejecución: guardar estado y enviar resumen
        logger.info("Primera ejecución - Guardando estado inicial")
        guardar_estado(items_actuales, hash_actual)
        enviar_resumen_inicial(items_actuales)
        return
    
    hash_guardado = estado_guardado.get('hash')
    
    if hash_actual == hash_guardado:
        logger.info("No hay cambios detectados")
        return
    
    # Hay cambios: encontrar los nuevos items
    logger.info("¡Cambios detectados! Comparando...")
    
    items_guardados = estado_guardado.get('items', [])
    
    # Crear sets de URLs para comparación rápida
    urls_guardadas = {item['url'] for item in items_guardados}
    urls_actuales = {item['url'] for item in items_actuales}
    
    # Nuevos items = están en actuales pero no en guardados
    nuevos_items = [item for item in items_actuales if item['url'] not in urls_guardadas]
    
    # Items eliminados = están en guardados pero no en actuales
    items_eliminados = [item for item in items_guardados if item['url'] not in urls_actuales]
    
    if nuevos_items:
        logger.info(f"Se encontraron {len(nuevos_items)} nuevos items")
        enviar_alerta_nuevos_items(nuevos_items)
    
    if items_eliminados:
        logger.info(f"Se eliminaron {len(items_eliminados)} items")
        # Opcional: notificar eliminaciones
        # enviar_alerta_eliminados(items_eliminados)
    
    # Guardar nuevo estado
    guardar_estado(items_actuales, hash_actual)


def monitorear():
    """Función principal de monitoreo"""
    logger.info(f"Iniciando monitoreo de {URL_BASE}")
    
    # Obtener contenido de la URL principal (redirige a oops)
    html = obtener_contenido_web("https://visuales.uclv.cu/")
    
    if not html:
        # Intentar directamente con la URL de redirección
        logger.info("Intentando con oops.uclv.edu.cu...")
        html = obtener_contenido_web(URL_BASE)
    
    if not html:
        logger.error("No se pudo obtener el contenido de la web")
        return
    
    # Extraer items del directorio
    items = extraer_items_directorio(html, URL_BASE)
    logger.info(f"Se encontraron {len(items)} items en total")
    
    # Calcular hash del contenido
    hash_content = obtener_hash_contenido(items)
    
    # Comparar y notificar cambios
    comparar_y_notificar(items, hash_content)


# ========== EJECUCIÓN ==========
if __name__ == "__main__":
    print("="*50)
    print("🔍 Monitor de Visuales UCLV")
    print("="*50)
    print(f"URL monitoreada: https://visuales.uclv.cu/")
    print(f"Archivo de estado: {ESTADO_FILE}")
    print("")
    
    monitorear()
    
    print("")
    print("✅ Monitoreo completado")