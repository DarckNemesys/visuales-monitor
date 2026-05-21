#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import urllib.parse
import requests
from pathlib import Path
from typing import List, Dict, Optional, Callable, Tuple
from datetime import datetime
from bs4 import BeautifulSoup

class URLUtils:
    @staticmethod
    def is_valid_url(url: str) -> bool:
        return url.startswith(('http://', 'https://'))
    
    @staticmethod
    def extract_folder_name(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path_parts = [part for part in parsed.path.split('/') if part]
        if path_parts:
            folder_name = urllib.parse.unquote(path_parts[-1])
            folder_name = re.sub(r'[<>:"/\\|?*]', '_', folder_name)
            return folder_name
        return "descarga_ucvl"
    
    @staticmethod
    def build_full_url(base_url: str, href: str) -> str:
        if href.startswith('http'):
            return href
        return urllib.parse.urljoin(base_url, href)

class FileUtils:
    VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'}
    SUBTITLE_EXTENSIONS = {'.srt', '.sub', '.vtt', '.ass', '.ssa'}
    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp'}
    INFO_EXTENSIONS = {'.nfo', '.txt', '.info'}
    
    @classmethod
    def get_file_type(cls, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext in cls.VIDEO_EXTENSIONS: return "video"
        elif ext in cls.SUBTITLE_EXTENSIONS: return "subtitle"
        elif ext in cls.IMAGE_EXTENSIONS: return "image"
        elif ext in cls.INFO_EXTENSIONS: return "info"
        return "other"
    
    @staticmethod
    def format_file_size(size_bytes: int) -> str:
        if size_bytes == 0: return "0 B"
        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        size = float(size_bytes)
        while size >= 1024.0 and i < len(size_names) - 1:
            size /= 1024.0
            i += 1
        return f"{size:.1f} {size_names[i]}"
    
    @staticmethod
    def clean_filename(filename: str) -> str:
        filename = urllib.parse.unquote(filename)
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        return filename

def scrape_folder(url: str, recursive: bool = False, max_depth: int = 1) -> List[Dict]:
    items = []
    if not url.endswith('/'): url += '/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
    except Exception:
        return items
        
    soup = BeautifulSoup(response.text, 'html.parser')
    for a in soup.find_all('a'):
        href = a.get('href')
        if not href or href in ['../', './'] or href.startswith('?'): continue
        full_url = URLUtils.build_full_url(url, href)
        if href.endswith('/') and recursive and max_depth > 0:
            items.extend(scrape_folder(full_url, recursive, max_depth - 1))
        elif not href.endswith('/'):
            items.append({
                'name': href, 'url': full_url,
                'type': FileUtils.get_file_type(href),
                'timestamp': datetime.now().isoformat()
            })
    return items

def descargar_archivo(url: str, destino: str, progress_callback: Optional[Callable] = None, max_retries: int = 3) -> Tuple[str, int]:
    nombre = os.path.basename(urllib.parse.unquote(url.split('?')[0]))
    if not nombre or '.' not in nombre: nombre = f"descarga_{int(time.time())}"
    nombre = FileUtils.clean_filename(nombre)
    ruta = os.path.join(destino, nombre)
    
    for intento in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, stream=True, timeout=120, headers=headers)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            if 'text/html' in response.headers.get('content-type', '') and total_size < 150000:
                if 'visuales.uclv.cu' in url:
                    return descargar_archivo(url.replace('visuales.uclv.cu', 'oops.uclv.edu.cu'), destino, progress_callback, max_retries)
                raise Exception("El servidor retornó un HTML de denegación.")
            downloaded = 0
            with open(ruta, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded, total_size, nombre)
            return ruta, total_size
        except Exception as e:
            if intento == max_retries - 1: raise e
            time.sleep(4)
    raise Exception("Fallo en la descarga tras reintentos.")
