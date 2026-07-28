#!/usr/bin/env python3
"""
monitor.py — Sistema di monitoraggio piogge per le aree configurate.

Esegue ogni 15 minuti (via GitHub Actions). Per ogni area con
`monitoring.enabled=true` in areas.json:

1. Scarica l'ultimo SRT1 (cumulata oraria) dall'API DPC
2. Calcola media area + max dentro il poligono
3. Confronta con le soglie configurate
4. Per ogni soglia ATTRAVERSATA IN SALITA (anti-spam): genera evento
5. Scarica forecast OpenMeteo per il centroide area (prossime N ore)
6. Invia notifiche: email + Telegram
7. Salva stato + evento in CSV

Secrets richiesti (GitHub Actions → Settings → Secrets and variables → Actions):
  SMTP_HOST          es. smtp.gmail.com
  SMTP_PORT          es. 587
  SMTP_USER          tuo.indirizzo@gmail.com
  SMTP_PASS          App password Gmail (16 caratteri, senza spazi)
  SMTP_TO            destinatario notifiche (uno o più, separati da virgola)
  TELEGRAM_TOKEN     token bot Telegram (da @BotFather)
  TELEGRAM_CHAT_ID   chat ID (proprio user ID o ID di un gruppo)

Se un secret manca, il canale corrispondente viene saltato (warning a log).
"""

import argparse
import csv
import io
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import numpy as np
import rasterio
import rasterio.mask
import requests
from pyproj import Transformer
from rasterio.windows import Window
from shapely.geometry import Polygon, mapping

# ─── Config & logging ────────────────────────────────────────────────────────

DPC_API = 'https://radar-api.protezionecivile.it'
OPENMETEO_API = 'https://api.open-meteo.com/v1/forecast'
METNO_API = 'https://api.met.no/weatherapi/locationforecast/2.0/compact'
METNO_UA = 'radar-dpc-monitor/1.0 github.com/ebbenetriglav/radar-dpc'
USER_AGENT = 'Mozilla/5.0 (radar-dpc-monitor/1.0)'
HTTP_TIMEOUT = 30

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
log = logging.getLogger('monitor')

EVENT_HEADERS = [
    'event_timestamp_utc',
    'area_name',
    'level',                  # warning / alarm / emergency
    'threshold_mm',
    'observed_mm_mean',
    'observed_mm_max',
    'product',
    'observation_timestamp_utc',
    'forecast_max_6h_mm',
    'notified_email',         # true/false/skipped
    'notified_telegram',      # true/false/skipped
    'note',
]

# ─── HTTP session ───────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update({'User-Agent': USER_AGENT, 'Accept': '*/*'})

def _http(method, url, **kw):
    kw.setdefault('timeout', HTTP_TIMEOUT)
    for attempt in range(3):
        try:
            r = _session.request(method, url, **kw)
            if r.status_code < 500:
                return r
            log.warning(f'  HTTP {method} {r.status_code} (attempt {attempt+1})')
        except Exception as e:
            log.warning(f'  HTTP {method} fallito (attempt {attempt+1}): {e}')
        time.sleep(2 * (attempt + 1))
    return None


# ─── DPC API: ultimo SRT1 + GeoTIFF ──────────────────────────────────────────

def get_last_product(product_type):
    r = _http('GET', f'{DPC_API}/findLastProductByType', params={'type': product_type})
    if not r or not r.ok:
        return None
    data = r.json()
    items = data.get('lastProducts', [])
    return items[0] if items else None


def get_pre_signed_url(product_type, product_date_ms):
    r = _http('POST', f'{DPC_API}/downloadProduct',
              json={'productType': product_type, 'productDate': product_date_ms},
              headers={'Content-Type': 'application/json'})
    if not r or not r.ok:
        return None
    try:
        return r.json().get('url')
    except Exception:
        return None


def download_geotiff(url):
    r = _http('GET', url)
    if not r or not r.ok or len(r.content) < 256:
        return None
    return r.content


# ─── Catch-up frame: recupero buchi dello scheduler GitHub ───────────────────
# I cron GitHub Actions vengono ritardati/saltati sotto carico (osservato:
# monitor */15 eseguito ogni 3-4h la notte del 20/07/2026 → cella 40-50mm su
# Ruspino non valutata). Rimedio: ad ogni run valutiamo TUTTI i frame dal
# precedente frame valutato (state '<area>:<prod>:last_eval_ms') fino
# all'ultimo disponibile, con passo pari alla griglia del prodotto e cap
# CATCHUP_MAX_H. L'API DPC downloadProduct accetta productDate storici
# (stesso pattern già usato da collect.py per il backfill).
#
# Passi campionamento (dichiarati, non ipotesi):
#   SRT1: griglia 5' osservata nei dati; passo catch-up 10'. SRT1 è cumulata
#         MOBILE oraria: un superamento di soglia persiste per costruzione su
#         più frame consecutivi, quindi il passo 10' può perdere solo
#         sfioramenti marginali di durata <10'.
#   CUM3: timestamp osservati a :00/:30 → passo 30'.
CATCHUP_MAX_H = float(os.environ.get('CATCHUP_MAX_H', '4'))
CATCHUP_STEP_MIN = {'SRT1': 10, 'CUM3': 30}
LATE_MARK_MIN = 30   # frame più vecchio di così → notifica marcata "tardiva"


def build_catchup_frames(area_name, product, latest_ms, state):
    """Lista [(ts_ms, ts_iso, late_min)] in ordine cronologico, ultimo incluso.
    late_min = minuti tra frame e adesso (per marcare notifiche tardive)."""
    step_ms = CATCHUP_STEP_MIN.get(product, 30) * 60_000
    key = f'{area_name}:{product}:last_eval_ms'
    last_eval = state.get(key)
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    cap_ms = latest_ms - int(CATCHUP_MAX_H * 3600_000)

    frames_ms = [latest_ms]
    if isinstance(last_eval, (int, float)) and last_eval < latest_ms:
        t = latest_ms - step_ms
        while t > max(last_eval, cap_ms):
            frames_ms.append(int(t))
            t -= step_ms
    frames_ms.sort()

    out = []
    for ms in frames_ms:
        iso = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        late_min = int((now_ms - ms) / 60_000)
        out.append((ms, iso, late_min))
    if len(out) > 1:
        log.info(f'  [{area_name}/{product}] catch-up: {len(out)} frame da valutare '
                 f'(gap scheduler, ultimo valutato: {last_eval})')
    return out


# ─── Stats area ──────────────────────────────────────────────────────────────

def stats_for_polygon(tiff_bytes, polygon_latlon):
    poly = Polygon([(lon, lat) for lat, lon in polygon_latlon])
    geojson = mapping(poly)
    with rasterio.open(io.BytesIO(tiff_bytes)) as src:
        try:
            masked, _ = rasterio.mask.mask(src, [geojson], crop=True, nodata=src.nodata)
        except ValueError:
            return None
        nodata = src.nodata if src.nodata is not None else -9999
    arr = masked[0]
    valid = (arr != nodata) & np.isfinite(arr) & (arr > -900)
    vals = arr[valid]
    if vals.size == 0:
        return None
    return {
        'mean': float(np.mean(vals)),
        'max':  float(np.max(vals)),
        'count': int(vals.size),
    }


# ─── Forecast OpenMeteo ──────────────────────────────────────────────────────

# Transformer WGS84 → TM custom DPC (usato per nowcasting VMI)
_TM_PROJ = '+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs'
_wgs84_to_tm = Transformer.from_crs('EPSG:4326', _TM_PROJ, always_xy=True)


def fetch_wind(lat, lon):
    """Direzione vento istantanea (gradi 0-360, 0=N, 90=E) e velocità da OpenMeteo."""
    try:
        r = _http('GET', OPENMETEO_API, params={
            'latitude': lat, 'longitude': lon,
            'current': 'wind_direction_10m,wind_speed_10m',
            'timezone': 'UTC',
        })
        if not r or not r.ok: return None
        d = r.json().get('current', {})
        return {
            'direction_deg': d.get('wind_direction_10m'),
            'speed_kmh':     d.get('wind_speed_10m'),
        }
    except Exception:
        return None


def _bearing_to_compass(deg):
    if deg is None: return '?'
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[round(deg / 22.5) % 16]


def check_nearby_storm(area, buffer_km, dbz_thresholds):
    """
    Scarica l'ultimo VMI e calcola max riflettività in un buffer (quadrato approssimato)
    di buffer_km attorno al centroide. Confronta con dbz_thresholds e restituisce
    eventuali trigger. Aggiunge direzione vento per indicare da dove arriva.
    """
    last = get_last_product('VMI')
    if not last:
        log.warning('  no last VMI')
        return None, None
    tiff = download_geotiff(get_pre_signed_url('VMI', last['time']))
    if not tiff:
        return None, None

    cx_tm, cy_tm = _wgs84_to_tm.transform(area['centroid']['lon'], area['centroid']['lat'])

    try:
        with rasterio.open(io.BytesIO(tiff)) as src:
            row, col = src.index(cx_tm, cy_tm)
            pixel_size = abs(src.transform.a)
            buffer_px = max(1, int(buffer_km * 1000 / pixel_size))
            win = Window(col - buffer_px, row - buffer_px, 2 * buffer_px, 2 * buffer_px)
            win = win.intersection(Window(0, 0, src.width, src.height))
            data = src.read(1, window=win)
            nodata = src.nodata if src.nodata is not None else -9999
    except Exception as e:
        log.warning(f'  VMI read fallito: {e}')
        return None, None

    valid = data[(data != nodata) & np.isfinite(data) & (data > -900) & (data < 1000)]
    if valid.size == 0:
        return {'max_dbz': None, 'pct_strong': 0.0, 'buffer_km': buffer_km,
                'timestamp_utc': datetime.fromtimestamp(last['time']/1000, tz=timezone.utc)
                                          .isoformat().replace('+00:00','Z')}, []

    max_dbz = float(np.max(valid))
    # Percentuale pixel sopra soglia minima (warning) per misurare "estensione"
    min_thr = min(t['value_dbz'] for t in dbz_thresholds) if dbz_thresholds else 35
    pct_strong = float(np.sum(valid >= min_thr) / valid.size * 100)

    summary = {
        'max_dbz':       max_dbz,
        'pct_strong':    pct_strong,
        'buffer_km':     buffer_km,
        'timestamp_utc': datetime.fromtimestamp(last['time']/1000, tz=timezone.utc)
                                  .isoformat().replace('+00:00','Z'),
    }

    triggers = []
    for th in sorted(dbz_thresholds, key=lambda x: x['value_dbz']):
        if max_dbz >= th['value_dbz']:
            triggers.append({**th, 'observed_dbz': max_dbz})
    return summary, triggers


def evaluate_storm_triggers(area, triggers, state, now_iso, anti_spam_min):
    """Anti-spam per nowcasting radar (chiave: area:storm:level)."""
    new_triggers = []
    for tr in triggers:
        key = f"{area['name']}:storm:{tr['level']}"
        st = state.get(key, {'active': False, 'last_trigger_utc': None, 'last_below_utc': None})
        if not st['active']:
            st['active'] = True
            st['last_trigger_utc'] = now_iso
            new_triggers.append(tr)
        state[key] = st
    # Riarmo soglie storm non più triggerate in questo run
    levels_now = {t['level'] for t in triggers}
    for key in list(state.keys()):
        if not key.startswith(f"{area['name']}:storm:"): continue
        lv = key.rsplit(':', 1)[1]
        if lv not in levels_now and state[key].get('active'):
            if not state[key].get('last_below_utc'):
                state[key]['last_below_utc'] = now_iso
            else:
                last_below = datetime.fromisoformat(state[key]['last_below_utc'].replace('Z','+00:00'))
                now_dt = datetime.fromisoformat(now_iso.replace('Z','+00:00'))
                if (now_dt - last_below).total_seconds() >= anti_spam_min * 60:
                    state[key] = {'active': False, 'last_trigger_utc': None, 'last_below_utc': None}
    return new_triggers


def compose_messages_storm(area, trigger, summary, wind):
    """Compone messaggi per nowcasting radar (cella convettiva nei dintorni)."""
    label = area['label']
    lvl   = trigger['level']
    icon  = trigger['icon']
    dbz   = trigger['observed_dbz']
    thr   = trigger['value_dbz']
    pct   = summary['pct_strong']
    buf   = summary['buffer_km']

    wind_str = ''
    if wind and wind.get('direction_deg') is not None:
        wind_str = f"Vento attuale: da {_bearing_to_compass(wind['direction_deg'])} ({wind['direction_deg']:.0f}°), {wind.get('speed_kmh', '?')} km/h.\n"

    text = (
        f"⛈️ TEMPORALE NEI DINTORNI — {label} — {lvl.upper()}\n\n"
        f"Riflettività VMI radar DPC entro {buf} km dal centroide:\n"
        f"  • Max osservato: {dbz:.1f} dBZ (soglia {thr} dBZ)\n"
        f"  • Pixel sopra soglia: {pct:.1f}% del buffer\n\n"
        f"{wind_str}"
        f"Sorgente: VMI Protezione Civile (timestamp {summary['timestamp_utc']})\n"
    )
    md = (
        f"{icon} *TEMPORALE — {label}*\n"
        f"Livello: *{lvl.upper()}* (radar DPC)\n"
        f"VMI max: *{dbz:.1f} dBZ* in {buf} km · soglia {thr}\n"
        f"Estensione: {pct:.1f}% pixel\n"
    )
    if wind:
        md += f"\nVento: da {_bearing_to_compass(wind['direction_deg'])} ({wind.get('speed_kmh','?')} km/h)"

    color = {'warning': '#e0a800', 'alarm': '#e85e2c', 'emergency': '#c41e3a'}.get(lvl, '#888')
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
      <div style="background:{color};color:white;padding:12px 18px;border-radius:6px 6px 0 0">
        <h2 style="margin:0">⛈️ {label} — {lvl.upper()} <span style="font-size:14px;font-weight:normal">(nowcast radar)</span></h2>
      </div>
      <div style="border:1px solid #ddd;border-top:0;padding:18px;border-radius:0 0 6px 6px">
        <p>VMI radar DPC entro <b>{buf} km</b> dal centroide:</p>
        <ul>
          <li>Max riflettività: <b>{dbz:.1f} dBZ</b> (soglia {thr} dBZ)</li>
          <li>Pixel sopra soglia: <b>{pct:.1f}%</b></li>
        </ul>
        {f'<p>{wind_str}</p>' if wind_str else ''}
        <p style="font-size:11px;color:#888">Timestamp: {summary['timestamp_utc']}</p>
      </div>
    </div>
    """
    subject = f"⛈️ {label} — TEMPORALE {lvl.upper()} VMI {dbz:.0f} dBZ entro {buf}km"
    return subject, text, html, md


def fetch_forecast(lat, lon, hours=6):
    """
    Restituisce metriche forecast nelle prossime N ore (granularità 15 min).
      max_1h_next: max cumulata rolling 1h nella finestra
      max_3h_next: max cumulata rolling 3h nella finestra
      total_period: somma totale del periodo
    """
    try:
        r = _http('GET', OPENMETEO_API, params={
            'latitude': lat,
            'longitude': lon,
            'minutely_15': 'precipitation',
            'forecast_minutes': hours * 60,
            'timezone': 'UTC',
        })
        if not r or not r.ok:
            return None
        data = r.json()
        prec = data.get('minutely_15', {}).get('precipitation', [])
        if not prec:
            return None
        # somma rolling 1h (4 step da 15 min) e 3h (12 step) per stimare picchi previsti
        def rolling_max(values, window):
            mx = 0.0
            for i in range(len(values) - window + 1):
                s = sum(v for v in values[i:i+window] if v is not None)
                if s > mx:
                    mx = s
            return mx
        return {
            'max_1h_next': round(rolling_max(prec, 4),  2),
            'max_3h_next': round(rolling_max(prec, 12), 2),
            'total_period': round(sum(p or 0 for p in prec), 2),
            'horizon_hours': hours,
        }
    except Exception as e:
        log.warning(f'  forecast fallito: {e}')
        return None


def fetch_forecast_metno(lat, lon, hours=6):
    """
    Forecast MET Norway (api.met.no). Granularità oraria.
    Ritorna max_1h_next e max_3h_next (rolling) come OpenMeteo per confronto.
    """
    try:
        r = _http('GET', METNO_API,
                  params={'lat': round(lat, 4), 'lon': round(lon, 4)},
                  headers={'User-Agent': METNO_UA})
        if not r or not r.ok:
            return None
        ts = r.json().get('properties', {}).get('timeseries', [])
        if not ts:
            return None
        # Estrai precipitazione oraria per le prossime `hours` ore
        prec_1h = []
        for entry in ts[:hours + 1]:
            details = entry.get('data', {}).get('next_1_hours', {}).get('details', {})
            if 'precipitation_amount' in details:
                prec_1h.append(details['precipitation_amount'])
        if not prec_1h:
            return None
        max_1h = max(prec_1h)
        # rolling 3h
        max_3h = 0.0
        for i in range(len(prec_1h) - 2):
            s = sum(prec_1h[i:i+3])
            if s > max_3h: max_3h = s
        return {'max_1h_next': round(max_1h, 2), 'max_3h_next': round(max_3h, 2),
                'horizon_hours': hours}
    except Exception as e:
        log.warning(f'  MET Norway forecast fallito: {e}')
        return None


# ─── Notifiche: Email + Telegram ─────────────────────────────────────────────

def send_email(subject, body_text, body_html=None, to=None):
    """Invia email. Se `to` (lista o stringa con virgole) viene passato,
    override del default SMTP_TO. Permette destinatari per-area."""
    host = os.environ.get('SMTP_HOST')
    port = int(os.environ.get('SMTP_PORT', '587'))
    user = os.environ.get('SMTP_USER')
    pwd  = os.environ.get('SMTP_PASS')
    # Override: lista o stringa "a@x,b@y" → fallback a SMTP_TO
    if to:
        if isinstance(to, (list, tuple)):
            to = ','.join(to)
    else:
        to = os.environ.get('SMTP_TO')
    if not (host and user and pwd and to):
        log.info('  email: secrets/destinatari mancanti, skip')
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
        log.info(f'  email inviata a {to}')
        return 'true'
    except Exception as e:
        log.warning(f'  email fallita: {e}')
        return 'false'


def _tg_send_one(tok, chat, md):
    """Invia un Telegram a UNA chat con resilienza (ritorna True se consegnato):
    - 429 rate limit  -> rispetta retry_after e riprova (il bot e' condiviso tra
      monitor/nowcast/forecast: durante il maltempo la chat va in rate limit e
      i forecast venivano persi silenziosamente)
    - 5xx             -> backoff e riprova
    - 400 (Markdown non valido) -> riprova UNA volta in testo semplice, cosi' il
      messaggio arriva comunque (meglio senza grassetti che perso)
    Logga sempre il motivo del fallimento (prima erano silenziati)."""
    url = "https://api.telegram.org/bot" + tok + "/sendMessage"
    payload = {"chat_id": chat, "text": md, "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    plain_tried = False
    for attempt in range(4):
        try:
            r = _session.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except Exception as e:
            log.warning("  telegram %s: rete KO (%s), retry %d/4" % (chat, e, attempt + 1))
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 3))
            except Exception:
                wait = 3
            log.warning("  telegram %s: 429 rate limit, attendo %ds" % (chat, wait))
            time.sleep(min(wait, 30) + 1); continue
        if r.status_code == 400 and not plain_tried:
            plain_tried = True
            payload.pop("parse_mode", None)   # ritenta in testo semplice
            log.warning("  telegram %s: 400 (Markdown?), riprovo in testo semplice" % chat)
            continue
        if r.status_code >= 500:
            log.warning("  telegram %s: HTTP %d, retry %d/4" % (chat, r.status_code, attempt + 1))
            time.sleep(2 * (attempt + 1)); continue
        body = ""
        try:
            body = r.text[:200]
        except Exception:
            pass
        log.warning("  telegram %s: HTTP %d non recuperabile: %s" % (chat, r.status_code, body))
        return False
    log.warning("  telegram %s: non consegnato dopo i tentativi" % chat)
    return False


def send_telegram(text_markdown, chat_ids=None):
    """Invia Telegram. Se `chat_ids` (lista o stringa CSV) viene passato,
    override del default TELEGRAM_CHAT_ID. Invia ad ognuno; se uno fallisce
    continua con gli altri. Ritorna 'true' se almeno uno OK."""
    token = os.environ.get('TELEGRAM_TOKEN')
    if chat_ids:
        if isinstance(chat_ids, str):
            chat_ids = [c.strip() for c in chat_ids.split(',') if c.strip()]
    else:
        default = os.environ.get('TELEGRAM_CHAT_ID')
        chat_ids = [default] if default else []
    if not (token and chat_ids):
        log.info('  telegram: secrets/chat_ids mancanti, skip')
        return 'skipped'
    n_ok = sum(1 for chat in chat_ids if _tg_send_one(token, chat, text_markdown))
    if n_ok:
        log.info(f'  telegram inviato a {n_ok}/{len(chat_ids)} chat')
        return 'true'
    return 'false'


# ─── Stato anti-spam ─────────────────────────────────────────────────────────

def load_state(state_file):
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def save_state(state_file, state):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True))


# ─── Last observations: scrive ad ogni run uno snapshot per il frontend ──────
def update_last_observation(file, area_name, product, ts_iso, stats):
    data = {}
    if file.exists():
        try: data = json.loads(file.read_text())
        except Exception: data = {}
    data.setdefault(area_name, {})
    data[area_name][product] = {
        'timestamp_utc': ts_iso,
        'mean':  round(stats['mean'], 3),
        'max':   round(stats['max'],  3),
        'count': stats['count'],
        'updated_at_utc': datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2, sort_keys=True))


def update_forecast_observation(file, area_name, forecast, forecast_metno, now_iso):
    """Salva i forecast OpenMeteo + MET Norway per il frontend."""
    data = {}
    if file.exists():
        try: data = json.loads(file.read_text())
        except Exception: data = {}
    data.setdefault(area_name, {})
    data[area_name]['forecast'] = {
        'openmeteo':  {'max_1h': (forecast or {}).get('max_1h_next'),
                       'max_3h': (forecast or {}).get('max_3h_next')},
        'metno':      {'max_1h': (forecast_metno or {}).get('max_1h_next'),
                       'max_3h': (forecast_metno or {}).get('max_3h_next')},
        'horizon_hours': (forecast or forecast_metno or {}).get('horizon_hours', 6),
        'updated_at_utc': now_iso,
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2, sort_keys=True))


def update_storm_observation(file, area_name, summary):
    """Aggiunge il nowcasting VMI all'osservazione last per il frontend."""
    data = {}
    if file.exists():
        try: data = json.loads(file.read_text())
        except Exception: data = {}
    data.setdefault(area_name, {})
    data[area_name]['VMI_nowcast'] = {
        'timestamp_utc': summary.get('timestamp_utc'),
        'max_dbz':       summary.get('max_dbz'),
        'pct_strong':    summary.get('pct_strong'),
        'buffer_km':     summary.get('buffer_km'),
        'updated_at_utc': datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2, sort_keys=True))


# ─── Forecast trigger: confronta forecast 1h/3h con le soglie SRT1/CUM3 ──────
def evaluate_forecast_thresholds(area, products_cfg, forecast, forecast_metno, state, now_iso, anti_spam_min, rearm_pct):
    """
    DOPPIA CONFERMA: il trigger forecast parte solo se SIA OpenMeteo SIA MET Norway
    superano la soglia. Riduce drasticamente i falsi positivi.
    State key: '<area>:forecast:<product>:<level>'.
    """
    if not forecast:
        return []

    triggers = []
    pairs = [
        ('SRT1', forecast.get('max_1h_next'), (forecast_metno or {}).get('max_1h_next'), '1h prossime 6h'),
        ('CUM3', forecast.get('max_3h_next'), (forecast_metno or {}).get('max_3h_next'), '3h prossime 6h'),
    ]
    for product, fc_om, fc_metno, horizon in pairs:
        if fc_om is None or product not in products_cfg:
            continue
        ths = sorted(products_cfg[product]['thresholds'], key=lambda x: x['value_mm'])
        # Livello più alto superato da ENTRAMBI i modelli (doppia conferma)
        highest = None
        for th in ths:
            both = (fc_om >= th['value_mm']) and (fc_metno is not None and fc_metno >= th['value_mm'])
            if both:
                highest = th
        # Gestione stato per ciascun livello (riarmo) ma trigger SOLO per il più alto
        for th in ths:
            key = f"{area['name']}:forecast:{product}:{th['level']}"
            st = state.get(key, {'active': False, 'last_trigger_utc': None, 'last_below_utc': None})
            rearm_value = th['value_mm'] * rearm_pct / 100.0
            is_this_highest = (highest is not None and th['level'] == highest['level'])

            if is_this_highest:
                if not st['active']:
                    st['active'] = True
                    st['last_trigger_utc'] = now_iso
                    st['last_below_utc'] = None
                    triggers.append({**th, 'forecast': True, 'product': product,
                                     'horizon': horizon, 'forecast_value': fc_om,
                                     'forecast_metno': fc_metno})
            else:
                # livelli non-massimi: gestisci solo il riarmo, niente trigger
                if fc_om < rearm_value and st['active']:
                    if not st['last_below_utc']:
                        st['last_below_utc'] = now_iso
                    else:
                        last_below = datetime.fromisoformat(st['last_below_utc'].replace('Z','+00:00'))
                        now_dt = datetime.fromisoformat(now_iso.replace('Z','+00:00'))
                        if (now_dt - last_below).total_seconds() >= anti_spam_min * 60:
                            st['active'] = False
                            st['last_below_utc'] = None
                elif fc_om >= rearm_value:
                    st['last_below_utc'] = None
            state[key] = st
    return triggers


def _level_key(area_name, product, level):
    return f'{area_name}:{product}:{level}'


def evaluate_thresholds(area, product, thresholds, current_value, state, now_iso, anti_spam_min, rearm_pct):
    """
    Per ogni soglia decide se TRIGGERARE (nuova notifica) o RIARMARE.
    Ritorna: lista di soglie da notificare (dict).
    Modifica state in place.
    """
    triggers = []
    thresholds_sorted = sorted(thresholds, key=lambda x: x['value_mm'])

    for th in thresholds_sorted:
        key = _level_key(area['name'], product, th['level'])
        st = state.get(key, {'active': False, 'last_trigger_utc': None, 'last_below_utc': None})

        threshold_mm = th['value_mm']
        rearm_value  = threshold_mm * rearm_pct / 100.0

        if current_value >= threshold_mm:
            # Soglia superata
            if not st['active']:
                # Trigger nuovo: notifica
                st['active'] = True
                st['last_trigger_utc'] = now_iso
                st['last_below_utc'] = None
                triggers.append(th)
            # se già attiva → no-op (anti-spam)
        elif current_value < rearm_value:
            # Sotto la soglia di riarmo
            if st['active']:
                if not st['last_below_utc']:
                    st['last_below_utc'] = now_iso
                else:
                    # Verifica se siamo stati sotto per >= anti_spam_min
                    last_below = datetime.fromisoformat(st['last_below_utc'].replace('Z', '+00:00'))
                    now_dt = datetime.fromisoformat(now_iso.replace('Z', '+00:00'))
                    if (now_dt - last_below).total_seconds() >= anti_spam_min * 60:
                        # Riarma
                        st['active'] = False
                        st['last_below_utc'] = None
                        log.info(f"  soglia {th['level']} riarmata per {area['name']}")
        else:
            # In banda intermedia: reset del timer di riarmo ma soglia resta attiva
            st['last_below_utc'] = None

        state[key] = st

    return triggers


# ─── Composizione messaggi ───────────────────────────────────────────────────

def compose_messages_forecast(area, trigger, forecast, forecast_metno=None):
    """Componi messaggi per un trigger predittivo (OpenMeteo)."""
    label = area['label']
    lvl   = trigger['level']
    icon  = trigger['icon']
    val_mm = trigger['value_mm']
    product = trigger['product']
    horizon = trigger['horizon']
    fc_val = trigger['forecast_value']
    unit_label = 'mm/1h' if product == 'SRT1' else 'mm/3h'

    metno_val = trigger.get('forecast_metno')
    metno_line = f"Conferma MET Norway: {metno_val:.1f} {unit_label}\n" if metno_val is not None else ""
    text = (
        f"🔮 PREVISIONE PIOGGIA — {label} — livello {lvl.upper()}\n"
        f"(confermata da 2 modelli: OpenMeteo + MET Norway)\n\n"
        f"OpenMeteo prevede picco cumulata {horizon}:\n"
        f"  • Stima OpenMeteo: {fc_val:.1f} {unit_label}\n"
        f"  • {metno_line}"
        f"  • Soglia: {val_mm} {unit_label}\n\n"
        f"Forecast finestra {forecast['horizon_hours']}h totali:\n"
        f"  • Max 1h: {forecast['max_1h_next']:.1f} mm\n"
        f"  • Max 3h: {forecast['max_3h_next']:.1f} mm\n"
        f"  • Totale: {forecast['total_period']:.1f} mm\n\n"
        f"Sorgente: OpenMeteo nowcast (15-min granularity)\n"
    )
    md = (
        f"{icon} 🔮 *PREVISIONE — {label}*\n"
        f"Livello: *{lvl.upper()}* ({product} prevista)\n"
        f"Soglia: {val_mm} {unit_label}\n"
        f"OpenMeteo: *{fc_val:.1f}* {unit_label}"
        + (f" • MET Norway: *{metno_val:.1f}*" if metno_val is not None else "") + "\n"
        f"Orizzonte: {horizon}\n"
        f"_2 modelli concordi_"
    )
    color = {'warning': '#e0a800', 'alarm': '#e85e2c', 'emergency': '#c41e3a'}.get(lvl, '#888')
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
      <div style="background:{color};color:white;padding:12px 18px;border-radius:6px 6px 0 0">
        <h2 style="margin:0">🔮 {label} — {lvl.upper()} <span style="font-size:14px;font-weight:normal">(previsione)</span></h2>
      </div>
      <div style="border:1px solid #ddd;border-top:0;padding:18px;border-radius:0 0 6px 6px">
        <p>OpenMeteo prevede picco cumulata <b>{horizon}</b>:</p>
        <ul>
          <li>Stima: <b>{fc_val:.1f} {unit_label}</b></li>
          <li>Soglia: <b>{val_mm} {unit_label}</b></li>
        </ul>
        <p style="background:#f4f4f4;padding:8px;border-radius:4px;font-size:12px">
          Finestra {forecast['horizon_hours']}h — max 1h: {forecast['max_1h_next']:.1f} mm •
          max 3h: {forecast['max_3h_next']:.1f} mm • totale: {forecast['total_period']:.1f} mm
        </p>
        <p style="font-size:11px;color:#888">Sorgente: OpenMeteo nowcast</p>
      </div>
    </div>
    """
    subject = f"🔮 {label} — PREVISIONE {lvl.upper()} {product} ({fc_val:.1f} {unit_label} attesi)"
    return subject, text, html, md


def arpa_confirmation(area_name, archive_dir=None, max_age_min=20):
    """Legge l'ultimo record ARPA (<area>_arpa.csv). Se fresco (<max_age_min),
    ritorna stringa di conferma incrociata da inserire nei messaggi.
    Ritorna '' se area non coperta da ARPA, file assente o dato vecchio."""
    if area_name not in ('ruspino', 'cepina'):
        return ''
    try:
        base = archive_dir if archive_dir else Path(__file__).resolve().parents[1]
        f = Path(base) / 'data' / f'{area_name}_arpa.csv'
        if not f.exists():
            return ''
        last = None
        with f.open() as fh:
            rdr = csv.DictReader(fh)
            for row in rdr:
                if row.get('location_type') == 'area':
                    last = row
        if not last:
            return ''
        ts = datetime.fromisoformat(last['timestamp_utc'].replace('Z', '+00:00'))
        age_min = (datetime.now(tz=timezone.utc) - ts).total_seconds() / 60
        if age_min > max_age_min:
            return ''
        mmh = float(last['max_mmh'])
        if mmh >= 0.5:
            return f"Radar ARPA Lombardia CONFERMA: {mmh:.1f} mm/h max in area ({age_min:.0f} min fa)\n"
        return f"Radar ARPA Lombardia: nessuna pioggia rilevata in area ({age_min:.0f} min fa) — verifica discordanza\n"
    except Exception:
        return ''


def compose_messages(area, product, threshold, stats, observation_ts_iso, forecast):
    label = area['label']
    lvl   = threshold['level']
    icon  = threshold['icon']
    val_mm = threshold['value_mm']
    # unità leggibile per prodotto
    unit_label = {'SRT1': 'mm/1h', 'CUM3': 'mm/3h'}.get(product, 'mm')
    obs_mean = stats['mean']
    obs_max  = stats['max']

    # Convert observation_ts a ora locale (Europe/Rome) per leggibilità
    obs_dt = datetime.fromisoformat(observation_ts_iso.replace('Z', '+00:00'))
    # Roma: approssimo +1 inverno / +2 estate
    # Per precisione potrei usare zoneinfo, ma in Actions container è installato
    try:
        from zoneinfo import ZoneInfo
        obs_local = obs_dt.astimezone(ZoneInfo('Europe/Rome'))
        tz_label = obs_local.strftime('%Z')
    except Exception:
        obs_local = obs_dt + timedelta(hours=2)
        tz_label = 'CEST'

    obs_str = obs_local.strftime('%d/%m/%Y %H:%M ') + tz_label

    fc_line = ''
    if forecast:
        fc_line = f"Forecast prossime {forecast['horizon_hours']}h (OpenMeteo): max cumulata 1h prevista {forecast['max_1h_next']:.1f} mm — totale periodo {forecast['total_period']:.1f} mm.\n"

    obs_label = {'SRT1': 'Cumulata oraria (SRT1)', 'CUM3': 'Cumulata 3h (CUM3)'}.get(product, product)
    arpa_line = arpa_confirmation(area['name'])

    # Plaintext
    text = (
        f"{icon} ALLERTA PIOGGIA — {label} — livello {lvl.upper()}\n\n"
        f"{obs_label}:\n"
        f"  • Media area: {obs_mean:.1f} mm\n"
        f"  • Max area:   {obs_max:.1f} mm\n"
        f"Soglia superata: {val_mm} {unit_label}\n"
        f"Ora osservazione: {obs_str}\n\n"
        f"{fc_line}"
        f"{arpa_line}"
        f"Dati: API Protezione Civile — radar-api.protezionecivile.it\n"
    )

    # Telegram markdown
    md = (
        f"{icon} *ALLERTA — {label}*\n"
        f"Prodotto: *{product}* • Livello: *{lvl.upper()}*\n"
        f"Soglia: {val_mm} {unit_label}\n\n"
        f"Osservato:\n"
        f"  media: *{obs_mean:.1f} mm*\n"
        f"  max:   *{obs_max:.1f} mm*\n"
        f"_{obs_str}_\n"
    )
    if forecast:
        md += f"\nForecast {forecast['horizon_hours']}h: max 1h *{forecast['max_1h_next']:.1f} mm*"
    if arpa_line:
        md += f"\n{arpa_line.strip()}"

    # Email HTML (più leggibile in client moderni)
    color = {'warning': '#e0a800', 'alarm': '#e85e2c', 'emergency': '#c41e3a'}.get(lvl, '#888')
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px">
      <div style="background:{color};color:white;padding:12px 18px;border-radius:6px 6px 0 0">
        <h2 style="margin:0">{icon} {label} — {lvl.upper()}</h2>
      </div>
      <div style="border:1px solid #ddd;border-top:0;padding:18px;border-radius:0 0 6px 6px">
        <p>Cumulata oraria osservata (SRT1):</p>
        <ul>
          <li>Media area: <b>{obs_mean:.1f} mm</b></li>
          <li>Max area:   <b>{obs_max:.1f} mm</b></li>
        </ul>
        <p>Soglia superata: <b>{val_mm} mm</b><br>
           Ora osservazione: <i>{obs_str}</i></p>
        {f'<p style="background:#f4f4f4;padding:8px;border-radius:4px">{fc_line.strip()}</p>' if fc_line else ''}
        {f'<p style="background:#eef7ee;padding:8px;border-radius:4px">{arpa_line.strip()}</p>' if arpa_line else ''}
        <p style="font-size:11px;color:#888">Dati: API Protezione Civile</p>
      </div>
    </div>
    """

    subject = f"{icon} {label} — {lvl.upper()} {product} ({obs_mean:.1f} {unit_label})"
    return subject, text, html, md


# ─── Pipeline ────────────────────────────────────────────────────────────────

def process_area(area, archive_dir, events_writer):
    if not area.get('monitoring', {}).get('enabled'):
        return

    mon = area['monitoring']
    metric  = mon.get('metric', 'mean')
    anti_spam = int(mon.get('anti_spam_minutes', 30))
    rearm_pct = int(mon.get('rearm_below_pct', 50))

    # Backward compat: schema vecchio aveva product+thresholds top-level.
    # Lo converto in 'products' dict.
    if 'products' in mon:
        products_cfg = mon['products']
    else:
        prod_name = mon.get('product', 'SRT1')
        products_cfg = {prod_name: {'thresholds': mon.get('thresholds', [])}}

    state_file = archive_dir / 'state' / 'monitor_state.json'
    last_obs_file = archive_dir / 'data' / 'last_observations.json'
    state = load_state(state_file)
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

    # Forecast condiviso fra tutti i prodotti dell'area (chiamato max una volta)
    forecast = None
    forecast_metno = None
    forecast_done = False
    channels = set(mon.get('channels', []))
    recipients = mon.get('recipients', {}) or {}
    rcpt_email = recipients.get('email') or None
    rcpt_tg    = recipients.get('telegram_chat_ids') or None

    for product, prod_cfg in products_cfg.items():
        thresholds = prod_cfg.get('thresholds', [])
        if not thresholds:
            continue
        log.info(f'[{area["label"]}] monitoring {product} ({metric})')

        last = get_last_product(product)
        if not last:
            log.warning(f'  no last product for {product}')
            continue
        latest_ms = last['time']

        # ── Catch-up: valuta in ordine cronologico tutti i frame dal
        #    precedente valutato all'ultimo (recupera i buchi dello scheduler).
        frames = build_catchup_frames(area['name'], product, latest_ms, state)
        last_ok_ms = None
        for ts_ms, ts_iso, late_min in frames:
            url = get_pre_signed_url(product, ts_ms)
            if not url:
                if ts_ms != latest_ms:
                    log.info(f'  {product} {ts_iso}: frame non disponibile (skip)')
                continue
            tiff = download_geotiff(url)
            if not tiff:
                continue

            stats = stats_for_polygon(tiff, area['polygon'])
            if not stats:
                log.warning(f'  stats N/D per {product} {ts_iso}')
                continue
            is_late = late_min > LATE_MARK_MIN
            log.info(f'  {product} {ts_iso}: mean={stats["mean"]:.2f} max={stats["max"]:.2f} mm'
                     + (f' [tardivo +{late_min}min]' if is_late else ''))
            last_ok_ms = ts_ms

            # Salva ultima osservazione SOLO per il frame più recente
            if ts_ms == latest_ms:
                update_last_observation(last_obs_file, area['name'], product, ts_iso, stats)

            metric_value = stats[metric]
            triggers = evaluate_thresholds(area, product, thresholds, metric_value, state, ts_iso, anti_spam, rearm_pct)
            if not triggers:
                continue

            # Forecast on-demand (una volta sola)
            if not forecast_done and mon.get('forecast', {}).get('enabled'):
                forecast = fetch_forecast(area['centroid']['lat'], area['centroid']['lon'],
                                          hours=mon['forecast'].get('lookahead_hours', 6))
                forecast_metno = fetch_forecast_metno(area['centroid']['lat'], area['centroid']['lon'],
                                          hours=mon['forecast'].get('lookahead_hours', 6))
                forecast_done = True
                if forecast:
                    m = forecast_metno or {}
                    log.info(f'  forecast OM: 1h={forecast["max_1h_next"]:.1f} 3h={forecast["max_3h_next"]:.1f} | MET: 1h={m.get("max_1h_next","?")} 3h={m.get("max_3h_next","?")} mm')

            for th in triggers:
                subject, text, html, md = compose_messages(area, product, th, stats, ts_iso, forecast)
                if is_late:
                    # Onestà sui tempi: la soglia è stata superata NEL PASSATO,
                    # rilevata ora per gap dello scheduler GitHub.
                    subject = f'⏰ [TARDIVO +{late_min}min] ' + subject
                    text = (f'⏰ RILEVAMENTO TARDIVO: osservazione delle {ts_iso}, '
                            f'valutata con {late_min} min di ritardo (gap scheduler GitHub Actions).\n\n') + text
                    md = f'⏰ *RILEVAMENTO TARDIVO* (+{late_min}min, oss. {ts_iso})\n' + md
                email_status = send_email(subject, text, html, to=rcpt_email) if 'email' in channels else 'skipped'
                tg_status    = send_telegram(md, chat_ids=rcpt_tg) if 'telegram' in channels else 'skipped'
                events_writer.writerow({
                    'event_timestamp_utc':        now_iso,
                    'area_name':                  area['name'],
                    'level':                      th['level'],
                    'threshold_mm':               th['value_mm'],
                    'observed_mm_mean':           f"{stats['mean']:.3f}",
                    'observed_mm_max':            f"{stats['max']:.3f}",
                    'product':                    product,
                    'observation_timestamp_utc':  ts_iso,
                    'forecast_max_6h_mm':         f"{forecast['max_1h_next']:.2f}" if forecast else '',
                    'notified_email':             email_status,
                    'notified_telegram':          tg_status,
                    'note':                       f'rilevamento tardivo catch-up +{late_min}min' if is_late else '',
                })
                log.info(f'  ✓ trigger {product}/{th["level"]} @ {ts_iso}: email={email_status} telegram={tg_status}')

        # Avanza il puntatore all'ultimo frame VALUTATO CON SUCCESSO: se un
        # frame (anche l'ultimo) fallisce il download, il prossimo run lo
        # ritenta invece di saltarlo per sempre.
        if last_ok_ms is not None:
            state[f'{area["name"]}:{product}:last_eval_ms'] = last_ok_ms

    # ─── Forecast triggers (predittivi OpenMeteo) ────────────────────────────
    # Anche se non c'è stato nessun trigger osservato, il forecast viene caricato
    # qui se non già fatto, per valutarne le soglie predittive.
    if mon.get('forecast', {}).get('enabled') and not forecast_done:
        forecast = fetch_forecast(area['centroid']['lat'], area['centroid']['lon'],
                                  hours=mon['forecast'].get('lookahead_hours', 6))
        forecast_metno = fetch_forecast_metno(area['centroid']['lat'], area['centroid']['lon'],
                                  hours=mon['forecast'].get('lookahead_hours', 6))
        forecast_done = True

    # Salva i forecast (OpenMeteo + MET Norway) in last_observations per il frontend
    if forecast or forecast_metno:
        update_forecast_observation(last_obs_file, area['name'], forecast, forecast_metno, now_iso)

    if forecast:
        fc_triggers = evaluate_forecast_thresholds(area, products_cfg, forecast, forecast_metno, state, now_iso, anti_spam, rearm_pct)
        for tr in fc_triggers:
            subject, text, html, md = compose_messages_forecast(area, tr, forecast, forecast_metno)
            email_status = send_email(subject, text, html, to=rcpt_email) if 'email' in channels else 'skipped'
            tg_status    = send_telegram(md, chat_ids=rcpt_tg) if 'telegram' in channels else 'skipped'
            events_writer.writerow({
                'event_timestamp_utc':       now_iso,
                'area_name':                 area['name'],
                'level':                     'forecast_' + tr['level'],
                'threshold_mm':              tr['value_mm'],
                'observed_mm_mean':          '',
                'observed_mm_max':           '',
                'product':                   tr['product'],
                'observation_timestamp_utc': '',
                'forecast_max_6h_mm':        f"{tr['forecast_value']:.2f}",
                'notified_email':            email_status,
                'notified_telegram':         tg_status,
                'note':                      f"forecast {tr['horizon']}",
            })
            log.info(f"  ✓ forecast trigger {tr['product']}/{tr['level']}: email={email_status} telegram={tg_status}")

    # ─── Nowcasting radar (VMI nei dintorni) ─────────────────────────────────
    nowcast_cfg = mon.get('nowcast_radar', {})
    if nowcast_cfg.get('enabled'):
        buf_km = nowcast_cfg.get('buffer_km', 25)
        dbz_ths = nowcast_cfg.get('thresholds', [])
        if dbz_ths:
            log.info(f'[{area["label"]}] nowcasting VMI (buffer {buf_km}km)')
            storm_summary, raw_triggers = check_nearby_storm(area, buf_km, dbz_ths)
            if storm_summary:
                # Salva anche nowcasting in last_observations.json
                update_storm_observation(last_obs_file, area['name'], storm_summary)

                if raw_triggers:
                    fc_triggers = evaluate_storm_triggers(area, raw_triggers, state, now_iso, anti_spam)
                    if fc_triggers:
                        wind = fetch_wind(area['centroid']['lat'], area['centroid']['lon'])
                        for tr in fc_triggers:
                            subject, text, html, md = compose_messages_storm(area, tr, storm_summary, wind)
                            email_status = send_email(subject, text, html, to=rcpt_email) if 'email' in channels else 'skipped'
                            tg_status    = send_telegram(md, chat_ids=rcpt_tg) if 'telegram' in channels else 'skipped'
                            events_writer.writerow({
                                'event_timestamp_utc':       now_iso,
                                'area_name':                 area['name'],
                                'level':                     'storm_' + tr['level'],
                                'threshold_mm':              tr['value_dbz'],
                                'observed_mm_mean':          f"{tr['observed_dbz']:.2f}",
                                'observed_mm_max':           f"{tr['observed_dbz']:.2f}",
                                'product':                   'VMI',
                                'observation_timestamp_utc': storm_summary['timestamp_utc'],
                                'forecast_max_6h_mm':        '',
                                'notified_email':            email_status,
                                'notified_telegram':         tg_status,
                                'note':                      f"nowcast buffer={buf_km}km pct_strong={storm_summary['pct_strong']:.1f}%",
                            })
                            log.info(f"  ✓ storm trigger {tr['level']} ({tr['observed_dbz']:.1f} dBZ): email={email_status} telegram={tg_status}")

    save_state(state_file, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--areas-file', default=None)
    parser.add_argument('--data-dir',   default=None)
    parser.add_argument('--dry-run',    action='store_true',
                        help='Esegue tutto ma non invia notifiche')
    args = parser.parse_args()

    if args.dry_run:
        # Forza i secrets a None per skip canali
        for k in ['SMTP_HOST','SMTP_USER','SMTP_PASS','SMTP_TO','TELEGRAM_TOKEN','TELEGRAM_CHAT_ID']:
            os.environ.pop(k, None)
        log.info('=== DRY-RUN: nessuna notifica verrà inviata ===')

    script_dir = Path(__file__).resolve().parent
    archive_dir = script_dir.parent
    areas_file = Path(args.areas_file) if args.areas_file else archive_dir / 'areas.json'
    data_dir   = Path(args.data_dir)   if args.data_dir   else archive_dir / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)

    log.info(f'Loading areas from {areas_file}')
    config = json.loads(areas_file.read_text())
    areas = config['areas']

    enabled = [a for a in areas if a.get('monitoring', {}).get('enabled')]
    log.info(f'Aree monitorate: {[a["label"] for a in enabled]}')
    if not enabled:
        log.info('Nessuna area con monitoring attivo, esco.')
        return 0

    # ── Rilevamento gap scheduler: se dal run precedente sono passati più di
    #    GAP_NOTIFY_MIN minuti (cron */15 → atteso ~15'), avvisa su Telegram.
    #    Trasparenza: l'utente deve SAPERE quando il watchdog è stato cieco.
    gap_notify_min = int(os.environ.get('GAP_NOTIFY_MIN', '75'))
    try:
        hb_file = data_dir / 'last_observations.json'
        prev = json.loads(hb_file.read_text()).get('_monitor_last_run_utc') if hb_file.exists() else None
        if prev:
            prev_dt = datetime.fromisoformat(prev.replace('Z', '+00:00'))
            gap_min = int((datetime.now(tz=timezone.utc) - prev_dt).total_seconds() / 60)
            if gap_min > gap_notify_min:
                log.warning(f'GAP SCHEDULER: {gap_min} min dal run precedente ({prev})')
                send_telegram(f'⚠️ *Monitor radar-dpc*: gap scheduler di *{gap_min} min* '
                              f'(run precedente {prev}). Eseguo catch-up sui frame persi.')
    except Exception as e:
        log.warning(f'gap check failed: {e}')

    events_file = data_dir / 'events.csv'
    write_header = not events_file.exists()
    with open(events_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_HEADERS)
        if write_header:
            writer.writeheader()
        for area in enabled:
            try:
                process_area(area, archive_dir, writer)
            except Exception as e:
                log.error(f'[{area["label"]}] errore inatteso: {e}', exc_info=True)
                continue

    # Heartbeat: lo script ha girato (anche se il DPC non ha pubblicato dati nuovi).
    # Letto dall'healthcheck per distinguere "script fermo" da "dati DPC in ritardo".
    try:
        hb_file = archive_dir / 'data' / 'last_observations.json'
        hb = json.loads(hb_file.read_text()) if hb_file.exists() else {}
        hb['_monitor_last_run_utc'] = datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        hb_file.write_text(json.dumps(hb, indent=2, sort_keys=True))
    except Exception as e:
        log.warning(f'heartbeat write failed: {e}')

    log.info('Done.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log.error(f'FATAL: {e}', exc_info=True)
        sys.exit(1)
