#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Monitor de Visuales UCLV como Web Service con Scheduler Interno
Desplegable en Render como Web Service
Escanea automáticamente cada 6 horas sin necesidad de cron externo
"""

import os
import json
import time
import hashlib
import logging
import threading
from datetime import datetime
from urllib.parse import urljoin
from typing import Dict, List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup

# ========== CONFIGURACIÓN ==========
URL_BASE = os.environ.get("URL_BASE", "https://oops.uclv.edu.cu/")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Intervalo de escaneo en segundos (6 horas = 21600 segundos)
# Para pruebas: 60 segundos, para producción: 21600 segundos
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL", 21600))

# Archivo para guardar el estado
ESTADO_FILE = os.path.join(os.path.dirname(__file__), "estado_visuales.json")

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Crear archivo de estado si no existe
if not os.path.exists(ESTADO_FILE):
    with open(ESTADO_FILE, 'w') as f:
        json.dump({}, f)

# Variable para controlar el scheduler
scheduler_running = True

# ========== MODELOS DE DATOS ==========
class Item(BaseModel):
    nombre: str
    tipo: str
    url: str
    fecha: Optional[str] = None
    tamaño: Optional[str] = None

class EjecucionResponse(BaseModel):
    mensaje: str
    timestamp: str
    nuevos_items: int
    total_items: int

# ========== FUNCIONES DE SCRAPING ==========
def obtener_contenido_web(url: str) -> Optional[str]:
    """Obtiene el contenido HTML de la URL"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Error al obtener {url}: {e}")
        return None

def extraer_items_directorio(html: str, base_url: str) -> List[Dict]:
    """Extrae la lista de archivos y carpetas de un listado de directorio Apache"""
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href == '../' or href.startswith('?'):
            continue
        
        full_url = urljoin(base_url, href)
        
        if href.endswith('/'):
            tipo = 'carpeta'
            nombre = href.rstrip('/')
        else:
            tipo = 'archivo'
            nombre = href
        
        size = None
        date = None
        
        parent_row = a.find_parent('tr')
        if parent_row:
            celdas = parent_row.find_all('td')
            if len(celdas) >= 3:
                if celdas[1].text.strip():
                    date = celdas[1].text.strip()
                if celdas[2].text.strip():
                    size = celdas[2].text.strip()
        
        items.append({
            'nombre': nombre,
            'tipo': tipo,
            'url': full_url,
            'fecha': date,
            'tamaño': size
        })
    
    return items

def obtener_hash_contenido(items: List[Dict]) -> str:
    """Calcula un hash único del contenido"""
    items_str = json.dumps(items, sort_keys=True)
    return hashlib.md5(items_str.encode()).hexdigest()

def obtener_estado_guardado() -> Optional[Dict]:
    """Lee el estado guardado"""
    try:
        with open(ESTADO_FILE, 'r') as f:
            return json.load(f)
    except:
        return None

def guardar_estado(items: List[Dict], hash_content: str):
    """Guarda el estado actual"""
    estado = {
        'timestamp': datetime.now().isoformat(),
        'hash': hash_content,
        'total_items': len(items),
        'ultimo_escaneo': datetime.now().isoformat(),
        'proximo_escaneo': (datetime.now().timestamp() + SCAN_INTERVAL_SECONDS),
        'items': items
    }
    with open(ESTADO_FILE, 'w') as f:
        json.dump(estado, f, indent=2)
    logger.info(f"Estado guardado con {len(items)} items")

# ========== FUNCIONES DE TELEGRAM ==========
def enviar_mensaje_telegram(mensaje: str) -> bool:
    """Envía un mensaje al grupo/canal de Telegram"""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "AQUI_TU_TOKEN_DEL_BOT":
        logger.warning("Token de Telegram no configurado")
        return False
    
    if not CHAT_ID:
        logger.warning("CHAT_ID no configurado")
        return False
    
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
        return True
    except Exception as e:
        logger.error(f"Error al enviar mensaje: {e}")
        return False

def enviar_alerta_nuevos_items(nuevos_items: List[Dict]) -> bool:
    """Envía una alerta formateada con los nuevos items"""
    if not nuevos_items:
        return False
    
    nuevas_carpetas = [i for i in nuevos_items if i['tipo'] == 'carpeta']
    nuevos_archivos = [i for i in nuevos_items if i['tipo'] == 'archivo']
    
    mensaje = f"📢 *NUEVO CONTENIDO DETECTADO*\n\n"
    mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    mensaje += f"🔗 Fuente: `{URL_BASE}`\n\n"
    
    if nuevas_carpetas:
        mensaje += f"📁 *Nuevas carpetas ({len(nuevas_carpetas)}):*\n"
        for carpeta in nuevas_carpetas[:10]:
            mensaje += f"• `{carpeta['nombre']}`\n"
        if len(nuevas_carpetas) > 10:
            mensaje += f"• ... y {len(nuevas_carpetas) - 10} más\n"
        mensaje += "\n"
    
    if nuevos_archivos:
        mensaje += f"📄 *Nuevos archivos ({len(nuevos_archivos)}):*\n"
        for archivo in nuevos_archivos[:15]:
            tamaño = f" ({archivo['tamaño']})" if archivo['tamaño'] else ""
            mensaje += f"• `{archivo['nombre']}`{tamaño}\n"
        if len(nuevos_archivos) > 15:
            mensaje += f"• ... y {len(nuevos_archivos) - 15} más\n"
        mensaje += "\n"
    
    mensaje += f"💡 *Para descargar*, usa el comando:\n"
    mensaje += f"`/descargar <url_completa>` en el bot descargador."
    
    # También enviar el enlace directo a la web
    mensaje += f"\n\n🔗 *Ver en web:*\n`{URL_BASE}`"
    
    return enviar_mensaje_telegram(mensaje)

def enviar_resumen_inicial(items: List[Dict]) -> bool:
    """Envía un resumen completo del contenido actual"""
    carpetas = [i for i in items if i['tipo'] == 'carpeta']
    archivos = [i for i in items if i['tipo'] == 'archivo']
    
    mensaje = f"🤖 *Monitor de Visuales UCLV ACTIVADO*\n\n"
    mensaje += f"📊 *Estado inicial del repositorio:*\n"
    mensaje += f"📁 Carpetas: {len(carpetas)}\n"
    mensaje += f"📄 Archivos: {len(archivos)}\n"
    mensaje += f"🔗 URL base: `{URL_BASE}`\n\n"
    mensaje += f"⏰ *Escaneo automático:* Cada {SCAN_INTERVAL_SECONDS//3600} horas\n\n"
    mensaje += f"⚠️ *Importante:* Este bot monitoreará la web y te avisará cuando haya nuevo contenido.\n\n"
    mensaje += f"📌 *Para descargar cualquier archivo*, usa el comando `/descargar <url>`."
    
    return enviar_mensaje_telegram(mensaje)

def enviar_reporte_periodico(items: List[Dict], nuevos: int) -> bool:
    """Envía un reporte periódico del estado del monitor"""
    carpetas = [i for i in items if i['tipo'] == 'carpeta']
    archivos = [i for i in items if i['tipo'] == 'archivo']
    
    mensaje = f"🔄 *Reporte periódico del monitor*\n\n"
    mensaje += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    mensaje += f"📊 *Estado actual:*\n"
    mensaje += f"📁 Carpetas: {len(carpetas)}\n"
    mensaje += f"📄 Archivos: {len(archivos)}\n"
    mensaje += f"🆕 Nuevos desde último escaneo: {nuevos}\n\n"
    mensaje += f"⏰ Próximo escaneo: en {SCAN_INTERVAL_SECONDS//3600} horas"
    
    if nuevos == 0:
        mensaje += f"\n\n✅ No se detectaron cambios en este período."
    
    return enviar_mensaje_telegram(mensaje)

# ========== FUNCIÓN PRINCIPAL DE MONITOREO ==========
def monitorear(notificar_si_no_hay_cambios: bool = False) -> Dict:
    """
    Función principal de monitoreo.
    
    Args:
        notificar_si_no_hay_cambios: Si es True, envía un reporte aunque no haya cambios
    """
    logger.info(f"Iniciando monitoreo de {URL_BASE}")
    
    resultado = {
        'success': False,
        'nuevos_items': 0,
        'items_eliminados': 0,
        'total_items': 0,
        'mensaje': ''
    }
    
    # Obtener contenido
    html = obtener_contenido_web("https://visuales.uclv.cu/")
    if not html:
        logger.info("Intentando con oops.uclv.edu.cu...")
        html = obtener_contenido_web(URL_BASE)
    
    if not html:
        resultado['mensaje'] = "No se pudo obtener el contenido de la web"
        logger.error(resultado['mensaje'])
        return resultado
    
    # Extraer items
    items = extraer_items_directorio(html, URL_BASE)
    resultado['total_items'] = len(items)
    logger.info(f"Se encontraron {len(items)} items en total")
    
    # Calcular hash
    hash_content = obtener_hash_contenido(items)
    
    # Comparar con estado guardado
    estado_guardado = obtener_estado_guardado()
    
    if not estado_guardado or not estado_guardado.get('hash'):
        # Primera ejecución
        logger.info("Primera ejecución - Guardando estado inicial")
        guardar_estado(items, hash_content)
        enviar_resumen_inicial(items)
        resultado['success'] = True
        resultado['mensaje'] = "Primera ejecución completada. Estado inicial guardado."
        return resultado
    
    hash_guardado = estado_guardado.get('hash')
    
    if hash_content == hash_guardado:
        logger.info("No hay cambios detectados")
        resultado['success'] = True
        resultado['mensaje'] = "No hay cambios detectados"
        
        if notificar_si_no_hay_cambios:
            enviar_reporte_periodico(items, 0)
        
        return resultado
    
    # Hay cambios
    logger.info("¡Cambios detectados! Comparando...")
    items_guardados = estado_guardado.get('items', [])
    
    urls_guardadas = {item['url'] for item in items_guardados}
    urls_actuales = {item['url'] for item in items}
    
    nuevos_items = [item for item in items if item['url'] not in urls_guardadas]
    items_eliminados = [item for item in items_guardados if item['url'] not in urls_actuales]
    
    resultado['nuevos_items'] = len(nuevos_items)
    resultado['items_eliminados'] = len(items_eliminados)
    
    if nuevos_items:
        enviar_alerta_nuevos_items(nuevos_items)
    
    # Guardar nuevo estado
    guardar_estado(items, hash_content)
    resultado['success'] = True
    resultado['mensaje'] = f"Monitoreo completado. {len(nuevos_items)} nuevos items encontrados."
    
    return resultado

# ========== SCHEDULER ==========
def scheduler_loop():
    """Bucle del scheduler que ejecuta el monitor periódicamente"""
    global scheduler_running
    
    logger.info(f"Scheduler iniciado - Escaneo cada {SCAN_INTERVAL_SECONDS} segundos ({SCAN_INTERVAL_SECONDS//3600} horas)")
    
    # Ejecutar inmediatamente al iniciar
    logger.info("Ejecutando primer escaneo...")
    monitorear(notificar_si_no_hay_cambios=True)
    
    while scheduler_running:
        # Esperar el intervalo definido
        time.sleep(SCAN_INTERVAL_SECONDS)
        
        if scheduler_running:
            logger.info("="*50)
            logger.info("Ejecución automática programada")
            monitorear(notificar_si_no_hay_cambios=True)
            logger.info("="*50)

def iniciar_scheduler():
    """Inicia el scheduler en un hilo separado"""
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()
    logger.info("Scheduler iniciado en segundo plano")
    return scheduler_thread

# ========== APLICACIÓN FASTAPI ==========
app = FastAPI(
    title="Monitor de Visuales UCLV",
    description="Monitorea cambios en visuales.uclv.cu y envía alertas a Telegram",
    version="1.0.0"
)

@app.on_event("startup")
async def startup_event():
    """Se ejecuta cuando la aplicación inicia"""
    logger.info("Iniciando aplicación...")
    iniciar_scheduler()

@app.get("/")
async def root():
    """Endpoint raíz"""
    estado_guardado = obtener_estado_guardado()
    proximo_escaneo = None
    if estado_guardado and estado_guardado.get('proximo_escaneo'):
        tiempo_restante = estado_guardado['proximo_escaneo'] - datetime.now().timestamp()
        proximo_escaneo = f"{int(tiempo_restante // 3600)}h {int((tiempo_restante % 3600) // 60)}m"
    
    return {
        "nombre": "Monitor de Visuales UCLV",
        "status": "activo",
        "version": "1.0.0",
        "configuracion": {
            "intervalo_escaneo": f"{SCAN_INTERVAL_SECONDS//3600} horas",
            "intervalo_segundos": SCAN_INTERVAL_SECONDS,
            "url_base": URL_BASE
        },
        "proximo_escaneo": proximo_escaneo,
        "endpoints": [
            "/ - Información del servicio",
            "/health - Health check",
            "/estado - Ver estado actual",
            "/ejecutar - Ejecutar monitor (manual)",
            "/detener - Detener scheduler",
            "/iniciar - Iniciar scheduler"
        ]
    }

@app.get("/health")
async def health():
    """Health check para Render"""
    estado_guardado = obtener_estado_guardado()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "scheduler_activo": scheduler_running,
        "ultimo_escaneo": estado_guardado.get('timestamp') if estado_guardado else None,
        "total_items": estado_guardado.get('total_items', 0) if estado_guardado else 0,
        "proximo_escaneo_segundos": SCAN_INTERVAL_SECONDS
    }

@app.get("/estado")
async def get_estado():
    """Obtiene el estado actual del monitor"""
    estado = obtener_estado_guardado()
    if not estado:
        return JSONResponse(
            status_code=404,
            content={"error": "No hay estado guardado. Ejecuta el monitor primero."}
        )
    
    tiempo_restante = None
    if estado.get('proximo_escaneo'):
        tiempo_restante = estado['proximo_escaneo'] - datetime.now().timestamp()
        if tiempo_restante < 0:
            tiempo_restante = 0
    
    return {
        "ultimo_escaneo": estado.get('timestamp'),
        "total_items": estado.get('total_items', 0),
        "total_carpetas": len([i for i in estado.get('items', []) if i['tipo'] == 'carpeta']),
        "total_archivos": len([i for i in estado.get('items', []) if i['tipo'] == 'archivo']),
        "hash": estado.get('hash'),
        "scheduler_activo": scheduler_running,
        "intervalo_escaneo_horas": SCAN_INTERVAL_SECONDS // 3600,
        "proximo_escaneo_segundos": int(tiempo_restante) if tiempo_restante else None
    }

@app.get("/ejecutar")
async def ejecutar_monitor(background_tasks: BackgroundTasks):
    """Ejecuta el monitor manualmente"""
    logger.info("Ejecución manual solicitada")
    
    # Ejecutar en segundo plano para no bloquear la respuesta
    resultado = monitorear(notificar_si_no_hay_cambios=True)
    
    return {
        "mensaje": "Monitor ejecutado",
        "timestamp": datetime.now().isoformat(),
        "resultado": resultado
    }

@app.get("/detener")
async def detener_scheduler():
    """Detiene el scheduler (solo para administración)"""
    global scheduler_running
    scheduler_running = False
    logger.warning("Scheduler detenido manualmente")
    return {
        "mensaje": "Scheduler detenido",
        "timestamp": datetime.now().isoformat(),
        "nota": "Para reiniciar, usa /iniciar o reinicia el servicio"
    }

@app.get("/iniciar")
async def iniciar_scheduler_endpoint():
    """Inicia el scheduler (solo para administración)"""
    global scheduler_running
    if not scheduler_running:
        scheduler_running = True
        iniciar_scheduler()
        logger.info("Scheduler reiniciado manualmente")
        return {
            "mensaje": "Scheduler iniciado",
            "timestamp": datetime.now().isoformat()
        }
    return {
        "mensaje": "Scheduler ya estaba activo",
        "timestamp": datetime.now().isoformat()
    }

# ========== MAIN ==========
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Iniciando servidor en puerto {port}")
    logger.info(f"Intervalo de escaneo: {SCAN_INTERVAL_SECONDS} segundos ({SCAN_INTERVAL_SECONDS//3600} horas)")
    uvicorn.run(app, host="0.0.0.0", port=port)