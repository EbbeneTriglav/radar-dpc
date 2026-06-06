#!/usr/bin/env python3
"""
radar_common.py — Utility condivise per gli script radar-dpc.

Centralizza HTTP-retry, fetch GeoTIFF, stats su poligono, notifiche
email/Telegram, persistenza JSON di stato.

Gli script esistenti (monitor.py, nowcast.py, collect.py,
forecast_ensemble_alert.py) hanno le proprie copie storiche per
retrocompatibilità: si migrano uno alla volta importando da qui.

USO:
    from radar_common import (
        http_request, send_email, send_telegram,
        load_state, save_state,
        get_pre_signed_url, download_geotiff, stats_for_polygon,
    )

Variabili d'ambiente lette:
  HTTP: nessuna (timeout/retry hardcoded ma override-abili come argomenti)
  Email: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_TO
  Telegram: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import io
import json
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

import numpy as np
import requests
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask as rio_mask
from shapely.geometry import Polygon, mapping

log = logging.getLogger(__name__)

# ─── Costanti ────────────────────────────────────────────────────────────────
DPC_API = 'https://radar-api.protezionecivile.it'
USER_AGENT = 'Mozilla/5.0 (radar-dpc-common/1.0)'
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_BACKOFF = 5  # secondi base, raddoppia ad ogni retry

# ─── HTTP con retry ──────────────────────────────────────────────────────────
def http_request(method: str, url: str,
                 timeout: int = HTTP_TIMEOUT,
                 retries: int = HTTP_RETRIES,
                 backoff: int = HTTP_BACKOFF,
                 user_agent: str = USER_AGENT,
                 **kwargs) -> Optional[requests.Response]:
    """Richiesta HTTP con retry esponenziale.
    Ritorna l'oggetto Response (ok o meno) all'ultimo tentativo, o None
    su eccezione persistente.
    """
    headers = kwargs.pop('headers', {}) or {}
    headers.setdefault('User-Agent', user_agent)
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, timeout=timeout,
                                 headers=headers, **kwargs)
            if r.ok:
                return r
            log.warning(f'  HTTP {method} {url} → {r.status_code} (attempt {attempt+1}/{retries})')
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
        except Exception as e:
            last_exc = e
            log.warning(f'  HTTP {method} {url} fail {attempt+1}/{retries}: {e}')
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    if last_exc is not None:
        return None
    return r  # ultima Response non-ok

# ─── DPC: pre-signed URL e download GeoTIFF ──────────────────────────────────
def get_pre_signed_url(product_type: str, product_date_ms: int) -> Optional[str]:
    """Richiede a `/downloadProduct` la pre-signed URL S3 del prodotto."""
    r = http_request('POST', f'{DPC_API}/downloadProduct',
                     json={'productType': product_type, 'productDate': product_date_ms},
                     headers={'Content-Type': 'application/json'})
    if not r or not r.ok:
        return None
    try:
        return r.json().get('url')
    except Exception as e:
        log.warning(f'  downloadProduct {product_type}@{product_date_ms}: JSON parse {e}')
        return None


def download_geotiff(url: str) -> Optional[bytes]:
    """Scarica il GeoTIFF e ritorna i bytes. None su errore o file troppo piccolo."""
    r = http_request('GET', url)
    if not r or not r.ok or len(r.content) < 256:
        return None
    return r.content


def get_last_product(product_type: str) -> Optional[int]:
    """Ritorna il timestamp (ms) dell'ultimo prodotto disponibile per `product_type`."""
    r = http_request('GET', f'{DPC_API}/existingProducts',
                     params={'productType': product_type})
    if not r or not r.ok:
        return None
    try:
        items = r.json()
        if not items:
            return None
        return int(items[-1])
    except Exception as e:
        log.warning(f'  existingProducts {product_type}: {e}')
        return None

# ─── Stats su poligono (lat/lon) ─────────────────────────────────────────────
def stats_for_polygon(tiff_bytes: bytes, polygon_latlon: list,
                      nodata_threshold: float = -900) -> Optional[dict]:
    """Calcola mean/max/count dei pixel del GeoTIFF dentro il poligono.
    `polygon_latlon`: lista di [lat, lon]. Ritorna None se non ci sono pixel
    validi o se il poligono cade fuori dal raster.
    """
    poly = Polygon([(lon, lat) for lat, lon in polygon_latlon])
    geojson = mapping(poly)
    with MemoryFile(tiff_bytes) as mf:
        with mf.open() as src:
            try:
                masked, _ = rio_mask(src, [geojson], crop=True, filled=True)
            except ValueError as e:
                log.warning(f'  mask fallito: {e}')
                return None
            nodata = src.nodata if src.nodata is not None else -9999
    arr = masked[0]
    valid = (arr != nodata) & np.isfinite(arr) & (arr > nodata_threshold)
    vals = arr[valid]
    if vals.size == 0:
        return None
    return {
        'mean':  float(np.mean(vals)),
        'max':   float(np.max(vals)),
        'min':   float(np.min(vals)),
        'count': int(vals.size),
    }

# ─── Notifiche: email + Telegram ─────────────────────────────────────────────
def send_email(subject: str, body_text: str, body_html: Optional[str] = None) -> str:
    """Invia email via SMTP+TLS. Ritorna 'true' | 'false' | 'skipped'."""
    host = os.environ.get('SMTP_HOST')
    port = int(os.environ.get('SMTP_PORT', '587'))
    user = os.environ.get('SMTP_USER')
    pwd  = os.environ.get('SMTP_PASS')
    to   = os.environ.get('SMTP_TO')
    if not (host and user and pwd and to):
        log.info('  email: secrets mancanti, skip')
        return 'skipped'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = user
        msg['To']      = to
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        if body_html:
            msg.attach(MIMEText(body_html, 'html', 'utf-8'))
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, [x.strip() for x in to.split(',')], msg.as_string())
        log.info('  email inviata')
        return 'true'
    except Exception as e:
        log.warning(f'  email fallita: {e}')
        return 'false'


def send_telegram(text_markdown: str) -> str:
    """Invia messaggio Markdown via bot Telegram. Ritorna 'true' | 'false' | 'skipped'."""
    token = os.environ.get('TELEGRAM_TOKEN')
    chat  = os.environ.get('TELEGRAM_CHAT_ID')
    if not (token and chat):
        log.info('  telegram: secrets mancanti, skip')
        return 'skipped'
    try:
        r = http_request('POST', f'https://api.telegram.org/bot{token}/sendMessage', json={
            'chat_id': chat,
            'text': text_markdown,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': True,
        })
        if r and r.ok:
            log.info('  telegram inviato')
            return 'true'
        log.warning(f'  telegram HTTP {r.status_code if r else "?"}')
        return 'false'
    except Exception as e:
        log.warning(f'  telegram fallito: {e}')
        return 'false'

# ─── Persistenza JSON di stato ───────────────────────────────────────────────
def load_state(state_file) -> dict:
    """Carica JSON di stato; ritorna {} se file mancante o non parsabile."""
    p = Path(state_file)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_state(state_file, state: dict) -> None:
    """Salva JSON di stato (crea cartella parent se manca)."""
    p = Path(state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


# ─── Esportazioni pubbliche ──────────────────────────────────────────────────
__all__ = [
    'DPC_API', 'USER_AGENT', 'HTTP_TIMEOUT', 'HTTP_RETRIES', 'HTTP_BACKOFF',
    'http_request',
    'get_pre_signed_url', 'download_geotiff', 'get_last_product',
    'stats_for_polygon',
    'send_email', 'send_telegram',
    'load_state', 'save_state',
]
