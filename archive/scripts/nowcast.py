#!/usr/bin/env python3
"""
nowcast.py — Nowcasting radar DPC per le aree di studio.

Gira ogni 60 minuti (workflow nowcast.yml). Per ogni area monitorata:
  1. Scarica SRI (mm/h istantanea) — 2 frame consecutivi per il moto
  2. Scarica SRT1 (cumulata 1h) e CUM3 (cumulata 3h)
  3. Maschera due anelli buffer (5 km e 10 km dal BORDO del poligono)
  4. Calcola max SRI / SRT1 nei buffer e confronta con le soglie
  5. Localizza la cella più intensa (pixel max) → distanza e direzione dal bacino
  6. Stima vettore di spostamento confrontando 2 frame SRI consecutivi
  7. Stima probabilità di arrivo sul bacino (direzione del moto vs posizione)
  8. Se trigger: invia email + Telegram, logga in events.csv (level nowcast_*)
  9. SEMPRE: mergia osservazioni nowcast in last_observations.json (heartbeat)

Soglie (uguali per tutte le aree):
  SRI (mm/h):  warning 10, alarm 15, emergency 25
  SRT1 (mm/1h): warning 8, alarm 15, emergency 20

Segnale riportato: mm/h (SRI) + mm cumulati 3h (CUM3).
"""

import csv
import io
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import numpy as np
import rasterio
import rasterio.mask
import requests
from pyproj import Transformer
from shapely.geometry import Polygon, Point, mapping
from shapely.ops import transform as shp_transform

# ─── Config ──────────────────────────────────────────────────────────────────
DPC_API = 'https://radar-api.protezionecivile.it'
UA = 'radar-dpc-nowcast/1.0'
HTTP_TIMEOUT = 30

BUFFERS_KM = [5, 10]               # doppio anello dal bordo poligono
SRI_THRESHOLDS = [                 # mm/h istantanea
    {'level': 'warning',   'value': 10, 'icon': '🌧️'},
    {'level': 'alarm',     'value': 15, 'icon': '⛈️'},
    {'level': 'emergency', 'value': 25, 'icon': '⚡'},
]
SRT1_THRESHOLDS = [                # mm/1h
    {'level': 'warning',   'value': 8,  'icon': '🌧️'},
    {'level': 'alarm',     'value': 15, 'icon': '⛈️'},
    {'level': 'emergency', 'value': 20, 'icon': '⚡'},
]

TM_PROJ = '+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs'
_to_tm   = Transformer.from_crs('EPSG:4326', TM_PROJ, always_xy=True)
_to_wgs  = Transformer.from_crs(TM_PROJ, 'EPSG:4326', always_xy=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('nowcast')

EVENT_HEADERS = [
    'event_timestamp_utc', 'area_name', 'level', 'threshold_mm',
    'observed_mm_mean', 'observed_mm_max', 'product', 'observation_timestamp_utc',
    'forecast_max_6h_mm', 'notified_email', 'notified_telegram', 'note',
]

_session = requests.Session()
_session.headers.update({'User-Agent': UA, 'Accept': '*/*'})


# ─── HTTP helper ─────────────────────────────────────────────────────────────
def _http(method, url, **kw):
    kw.setdefault('timeout', HTTP_TIMEOUT)
    for attempt in range(3):
        try:
            r = _session.request(method, url, **kw)
            if r.status_code < 500:
                return r
        except Exception as e:
            log.warning(f'  HTTP {method} fail {attempt+1}: {e}')
        time.sleep(2 * (attempt + 1))
    return None


# ─── Prodotti radar DPC ──────────────────────────────────────────────────────
def get_last_products(product_type, n=2):
    """
    Ritorna gli ultimi n timestamp (ms) disponibili per il prodotto.

    L'API DPC (findLastProductByType) restituisce solo l'ULTIMO prodotto.
    Per ottenere n frame calcoliamo i timestamp precedenti usando il periodo
    di aggiornamento del prodotto (es. PT5M per SRI → ogni 5 minuti).
    """
    r = _http('GET', f'{DPC_API}/findLastProductByType',
              params={'type': product_type})
    if not r or not r.ok:
        log.warning(f'  findLastProductByType({product_type}) fallito: '
                    f'status={r.status_code if r else "None"}')
        return []

    data = r.json()
    items = data.get('lastProducts', [])
    if not items:
        log.warning(f'  findLastProductByType({product_type}): nessun prodotto')
        return []

    latest_ts = items[0]['time']
    period_str = items[0].get('period', 'PT5M')

    # Parsing ISO-8601 durata → minuti  (PT5M → 5, PT1H → 60, PT60M → 60)
    m_h = re.search(r'(\d+)H', period_str)
    m_m = re.search(r'(\d+)M', period_str)
    step_min = (int(m_h.group(1)) * 60 if m_h else 0) + \
               (int(m_m.group(1)) if m_m else 0)
    if step_min == 0:
        step_min = 5  # fallback SRI default

    step_ms = step_min * 60 * 1000
    timestamps = [latest_ts - i * step_ms for i in range(n)]

    ts_labels = [datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime('%H:%M:%S') for t in timestamps]
    log.info(f'  {product_type}: ultimo={ts_labels[0]}, periodo={period_str}, '
             f'timestamps richiesti={ts_labels}')

    return timestamps


def download_tiff(product_type, ts_ms):
    """Scarica il GeoTIFF per un prodotto e timestamp specifici."""
    r = _http('POST', f'{DPC_API}/downloadProduct',
              json={'productType': product_type, 'productDate': ts_ms},
              headers={'Content-Type': 'application/json'})
    if not r or not r.ok:
        log.warning(f'  downloadProduct({product_type}, {ts_ms}) → '
                    f'HTTP {r.status_code if r else "None"}')
        return None
    url = r.json().get('url')
    if not url:
        log.warning(f'  downloadProduct({product_type}, {ts_ms}) → nessun URL nella risposta')
        return None
    rr = _http('GET', url)
    if not rr or not rr.ok or len(rr.content) < 256:
        log.warning(f'  download GeoTIFF {product_type} {ts_ms} → '
                    f'status={rr.status_code if rr else "None"}, '
                    f'size={len(rr.content) if rr else 0}')
        return None
    return rr.content


# ─── Geometria ───────────────────────────────────────────────────────────────
def ring_buffer_tm(polygon_latlon, inner_km, outer_km):
    """Anello buffer (corona) tra inner_km e outer_km dal bordo, in coordinate TM."""
    poly_wgs = Polygon([(lon, lat) for lat, lon in polygon_latlon])
    poly_tm = shp_transform(lambda x, y: _to_tm.transform(x, y), poly_wgs)
    outer = poly_tm.buffer(outer_km * 1000)
    if inner_km <= 0:
        return outer
    inner = poly_tm.buffer(inner_km * 1000)
    return outer.difference(inner)


def stats_in_geom_tm(tiff_bytes, geom_tm):
    """Max + posizione del pixel massimo dentro geom (in TM)."""
    gj = mapping(geom_tm)
    with rasterio.open(io.BytesIO(tiff_bytes)) as src:
        try:
            masked, transform = rasterio.mask.mask(src, [gj], crop=True, nodata=src.nodata)
        except (ValueError, Exception):
            return None
        nodata = src.nodata if src.nodata is not None else -9999
    arr = masked[0]
    valid = (arr != nodata) & np.isfinite(arr) & (arr > -900) & (arr < 10000)
    if not valid.any():
        return {'max': 0.0, 'mean': 0.0, 'max_lat': None, 'max_lon': None}
    vals = arr[valid]
    max_val = float(np.max(vals))
    # posizione pixel max
    ij = np.unravel_index(np.argmax(np.where(valid, arr, -np.inf)), arr.shape)
    row, col = int(ij[0]), int(ij[1])
    x_tm, y_tm = rasterio.transform.xy(transform, row, col)
    lon, lat = _to_wgs.transform(x_tm, y_tm)
    return {'max': max_val, 'mean': float(np.mean(vals)),
            'max_lat': lat, 'max_lon': lon, 'max_xy_tm': (x_tm, y_tm)}


# ─── Stima moto ─────────────────────────────────────────────────────────────
def estimate_motion(tiff_now, tiff_prev, geom_tm, dt_minutes):
    """
    Stima direzione + velocità del moto confrontando il baricentro di riflettività
    di 2 frame SRI consecutivi dentro la geom buffer.
    Ritorna dict con bearing_deg, speed_kmh, compass oppure None.
    """
    def centroid_weighted(tiff):
        gj = mapping(geom_tm)
        with rasterio.open(io.BytesIO(tiff)) as src:
            try:
                m, tr = rasterio.mask.mask(src, [gj], crop=True, nodata=src.nodata)
            except Exception:
                return None
            nd = src.nodata if src.nodata is not None else -9999
        a = m[0]
        valid = (a != nd) & np.isfinite(a) & (a > 0) & (a < 10000)
        if not valid.any() or a[valid].sum() <= 0:
            return None
        rows, cols = np.where(valid)
        w = a[valid]
        r_c = np.average(rows, weights=w)
        c_c = np.average(cols, weights=w)
        x, y = rasterio.transform.xy(tr, r_c, c_c)
        return np.array([x, y])

    c_now = centroid_weighted(tiff_now)
    c_prev = centroid_weighted(tiff_prev)
    if c_now is None or c_prev is None:
        return None
    d = c_now - c_prev  # metri, in TM (x=Est, y=Nord)
    dist_m = float(np.hypot(*d))
    if dist_m < 200:  # spostamento trascurabile
        return {'bearing_deg': None, 'speed_kmh': 0.0, 'compass': 'stazionaria'}
    # bearing: 0=N, 90=E
    bearing = (np.degrees(np.arctan2(d[0], d[1]))) % 360
    speed_kmh = (dist_m / 1000) / (dt_minutes / 60) if dt_minutes > 0 else 0
    return {'bearing_deg': float(bearing), 'speed_kmh': round(speed_kmh, 1),
            'compass': _compass(bearing)}


def _compass(deg):
    if deg is None:
        return '?'
    dirs = ['N','NE','E','SE','S','SW','W','NW']
    return dirs[round(deg / 45) % 8]


def arrival_probability(cell_xy_tm, motion, centroid_latlon, buffer_outer_km):
    """
    Stima probabilità che la cella raggiunga il bacino:
    - se la cella si muove VERSO il centroide → prob alta
    - pesata su distanza e velocità
    Heuristica semplice, 0-100%.
    """
    if not motion or motion.get('bearing_deg') is None:
        return None
    cx, cy = _to_tm.transform(centroid_latlon['lon'], centroid_latlon['lat'])
    # vettore cella→centroide
    to_center = np.array([cx - cell_xy_tm[0], cy - cell_xy_tm[1]])
    dist_km = np.hypot(*to_center) / 1000
    if dist_km < 0.1:
        return 95
    # direzione del moto
    b = np.radians(motion['bearing_deg'])
    motion_vec = np.array([np.sin(b), np.cos(b)])  # x=Est(sin), y=Nord(cos)
    to_center_n = to_center / np.linalg.norm(to_center)
    align = float(np.dot(motion_vec, to_center_n))  # -1..1 (1 = punta dritto al centro)
    if align <= 0:
        return max(0, int(10 * (1 - dist_km / (buffer_outer_km + 5))))  # si allontana
    # combina allineamento + vicinanza + velocità
    prox = max(0, 1 - dist_km / (buffer_outer_km + 5))
    speed_factor = min(1, motion['speed_kmh'] / 30)
    prob = 100 * align * (0.5 + 0.5 * prox) * (0.5 + 0.5 * speed_factor)
    return int(max(0, min(100, prob)))


# ─── Notifiche ───────────────────────────────────────────────────────────────
def send_email(subject, text, html=None, to=None):
    h, port = os.environ.get('SMTP_HOST'), int(os.environ.get('SMTP_PORT', '587'))
    u, pw = os.environ.get('SMTP_USER'), os.environ.get('SMTP_PASS')
    if to:
        if isinstance(to, (list, tuple)):
            to = ','.join(to)
    else:
        to = os.environ.get('SMTP_TO')
    if not (h and u and pw and to):
        return 'skipped'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'], msg['From'], msg['To'] = subject, u, to
        msg.attach(MIMEText(text, 'plain', 'utf-8'))
        if html: msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP(h, port, timeout=20) as s:
            s.starttls(); s.login(u, pw)
            s.sendmail(u, [x.strip() for x in to.split(',')], msg.as_string())
        return 'true'
    except Exception as e:
        log.warning(f'  email fail: {e}'); return 'false'


def send_telegram(md, chat_ids=None):
    tok = os.environ.get('TELEGRAM_TOKEN')
    if chat_ids:
        if isinstance(chat_ids, str):
            chat_ids = [c.strip() for c in chat_ids.split(',') if c.strip()]
    else:
        d = os.environ.get('TELEGRAM_CHAT_ID')
        chat_ids = [d] if d else []
    if not (tok and chat_ids):
        return 'skipped'
    n_ok = 0
    for chat in chat_ids:
        try:
            r = _http('POST', f'https://api.telegram.org/bot{tok}/sendMessage',
                      json={'chat_id': chat, 'text': md, 'parse_mode': 'Markdown', 'disable_web_page_preview': True})
            if r and r.ok: n_ok += 1
        except Exception:
            pass
    return 'true' if n_ok else 'false'


# ─── State anti-spam ─────────────────────────────────────────────────────────
def load_state(f):
    if f.exists():
        try: return json.loads(f.read_text())
        except Exception: return {}
    return {}

def save_state(f, st):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=2, sort_keys=True))


# ─── Osservazioni (heartbeat — MERGE con dati esistenti ad OGNI run) ──────
def update_nowcast_obs(file, all_obs):
    """
    Aggiorna last_observations.json MERGENDO i dati nowcast con quelli esistenti.

    Il file è condiviso con altri script (monitor/forecast) che scrivono
    CUM3, SRT1, forecast, ecc. Il nowcast aggiunge/aggiorna SOLO:
      - chiavi globali: _nowcast_last_run_utc, _nowcast_sri_frames, ...
      - per ogni area: area['nowcast'] = { buffers, motion, triggered, ... }
    Tutto il resto (CUM3, SRT1, forecast) viene preservato.
    """
    # Leggi dati esistenti
    existing = {}
    if file.exists():
        try:
            existing = json.loads(file.read_text())
        except Exception:
            existing = {}

    # Aggiorna metadati nowcast (prefisso _nowcast_ per non confliggere)
    existing['_nowcast_last_run_utc'] = all_obs.get('_last_run_utc', '')
    existing['_nowcast_sri_frames'] = all_obs.get('_sri_frames', 0)
    existing['_nowcast_srt1_available'] = all_obs.get('_srt1_available', False)
    existing['_nowcast_cum3_available'] = all_obs.get('_cum3_available', False)

    # Per ogni area, mergia sotto la chiave "nowcast"
    for key, value in all_obs.items():
        if key.startswith('_'):
            continue  # skip metadati, già gestiti sopra
        # key è il nome area (es. "ruspino", "panna", "cepina")
        if key not in existing:
            existing[key] = {}
        existing[key]['nowcast'] = value

    # Scrivi il file
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(existing, indent=2, sort_keys=True, ensure_ascii=False))
    area_count = sum(1 for k in all_obs if not k.startswith('_'))
    log.info(f'  last_observations.json aggiornato (nowcast per {area_count} aree, dati esistenti preservati)')


# ─── Composizione messaggi ───────────────────────────────────────────────────
def compose(area, product, trigger, signal, motion, prob, buffer_km):
    label, lvl, icon = area['label'], trigger['level'], trigger['icon']
    thr, val = trigger['value'], signal['max']
    unit = 'mm/h' if product == 'SRI' else 'mm/1h'
    cell_pos = ''
    if signal.get('max_lat'):
        cell_pos = f"Cella max: {signal['max_lat']:.3f}, {signal['max_lon']:.3f}\n"
    mot = ''
    if motion:
        if motion.get('compass') == 'stazionaria':
            mot = "Movimento: stazionaria\n"
        elif motion.get('bearing_deg') is not None:
            mot = f"Movimento: verso {motion['compass']} a {motion['speed_kmh']} km/h\n"
    prob_str = f"Probabilità arrivo sul bacino: {prob}%\n" if prob is not None else ''
    cum3_str = f"Cumulata 3h attuale nel buffer: {signal.get('cum3_max', 0):.1f} mm\n" if 'cum3_max' in signal else ''

    text = (
        f"{icon} CELLA RADAR IN AVVICINAMENTO — {label} — {lvl.upper()}\n\n"
        f"Rilevata entro {buffer_km} km dal bacino (radar DPC):\n"
        f"  • {product} max: {val:.1f} {unit} (soglia {thr})\n"
        f"  • {cum3_str}"
        f"{cell_pos}{mot}{prob_str}\n"
        f"Timestamp: {signal['ts_iso']}\n"
    )
    md = (
        f"{icon} *CELLA RADAR — {label}*\n"
        f"Livello: *{lvl.upper()}* • entro *{buffer_km} km*\n"
        f"{product} max: *{val:.1f} {unit}* (soglia {thr})\n"
        + (f"Cumulata 3h: *{signal.get('cum3_max',0):.1f} mm*\n" if 'cum3_max' in signal else '')
        + (f"Moto: {motion['compass']} {motion['speed_kmh']}km/h\n" if motion and motion.get('bearing_deg') is not None else '')
        + (f"Prob. arrivo: *{prob}%*\n" if prob is not None else '')
    )
    subject = f"{icon} {label} — CELLA {lvl.upper()} {product} {val:.0f}{unit} entro {buffer_km}km"
    return subject, text, md


# ─── Elaborazione per area ───────────────────────────────────────────────────
def process_area(area, archive_dir, writer, state, now_iso, sri_frames, srt1_tiff, cum3_tiff):
    """
    Valuta i buffer per un'area usando i tiff già scaricati (condivisi).
    Ritorna un dict con le osservazioni per last_observations.json.
    """
    centroid = area['centroid']
    mon_cfg = area.get('monitoring', {})
    channels = set(mon_cfg.get('channels', ['email', 'telegram']))
    rcpt = mon_cfg.get('recipients', {}) or {}
    rcpt_email = rcpt.get('email') or None
    rcpt_tg    = rcpt.get('telegram_chat_ids') or None
    triggered = False

    # Raccolta osservazioni per heartbeat
    obs = {
        'timestamp_utc': now_iso,
        'triggered': False,
        'buffers': {},
        'motion': None,
    }

    for buf_km in BUFFERS_KM:
        inner = 0 if buf_km == BUFFERS_KM[0] else BUFFERS_KM[0]
        geom = ring_buffer_tm(area['polygon'], inner, buf_km)

        buf_obs = {}

        # SRI (istantaneo)
        sri_now_ts, sri_now_tiff = sri_frames[0]
        sri_stat = stats_in_geom_tm(sri_now_tiff, geom)
        # CUM3 nel buffer (per il segnale mm/3h)
        cum3_stat = stats_in_geom_tm(cum3_tiff, geom) if cum3_tiff else None

        if sri_stat:
            sri_stat['ts_iso'] = datetime.fromtimestamp(sri_now_ts/1000, tz=timezone.utc).isoformat().replace('+00:00','Z')
            if cum3_stat:
                sri_stat['cum3_max'] = cum3_stat['max']
            log.info(f'    buf {buf_km}km SRI max={sri_stat["max"]:.1f} mm/h, '
                     f'mean={sri_stat["mean"]:.2f} mm/h'
                     + (f', cum3_max={cum3_stat["max"]:.1f} mm' if cum3_stat else ''))

            buf_obs['sri_max'] = round(sri_stat['max'], 2)
            buf_obs['sri_mean'] = round(sri_stat['mean'], 2)
            if cum3_stat:
                buf_obs['cum3_max'] = round(cum3_stat['max'], 2)

            was_triggered = _eval_product(area, 'SRI', SRI_THRESHOLDS, sri_stat, geom, buf_km,
                          sri_frames, centroid, state, now_iso, channels, writer,
                          rcpt_email=rcpt_email, rcpt_tg=rcpt_tg)
            if was_triggered:
                triggered = True

        # SRT1 (1h)
        if srt1_tiff:
            srt1_stat = stats_in_geom_tm(srt1_tiff[1], geom)
            if srt1_stat:
                srt1_stat['ts_iso'] = datetime.fromtimestamp(srt1_tiff[0]/1000, tz=timezone.utc).isoformat().replace('+00:00','Z')
                if cum3_stat:
                    srt1_stat['cum3_max'] = cum3_stat['max']
                log.info(f'    buf {buf_km}km SRT1 max={srt1_stat["max"]:.1f} mm/1h, '
                         f'mean={srt1_stat["mean"]:.2f} mm/1h')

                buf_obs['srt1_max'] = round(srt1_stat['max'], 2)
                buf_obs['srt1_mean'] = round(srt1_stat['mean'], 2)

                was_triggered = _eval_product(area, 'SRT1', SRT1_THRESHOLDS, srt1_stat, geom, buf_km,
                              None, centroid, state, now_iso, channels, writer,
                              rcpt_email=rcpt_email, rcpt_tg=rcpt_tg)
                if was_triggered:
                    triggered = True

        obs['buffers'][f'{buf_km}km'] = buf_obs

    # Stima moto (sul buffer esterno) — per le osservazioni
    if len(sri_frames) >= 2:
        outer_geom = ring_buffer_tm(area['polygon'], 0, BUFFERS_KM[-1])
        dt_min = abs(sri_frames[0][0] - sri_frames[1][0]) / 60000
        motion = estimate_motion(sri_frames[0][1], sri_frames[1][1], outer_geom, dt_min)
        if motion:
            obs['motion'] = {
                'compass': motion['compass'],
                'speed_kmh': motion['speed_kmh'],
                'bearing_deg': motion.get('bearing_deg'),
            }

    obs['triggered'] = triggered
    return obs


def _eval_product(area, product, thresholds, signal, geom, buf_km,
                  sri_frames, centroid, state, now_iso, channels, writer,
                  rcpt_email=None, rcpt_tg=None):
    """Valuta soglie. Ritorna True se ha triggerato, False altrimenti."""
    # determina trigger massimo superato
    hit = None
    for th in sorted(thresholds, key=lambda x: x['value']):
        if signal['max'] >= th['value']:
            hit = th
    if not hit:
        return False

    key = f"{area['name']}:nowcast:{product}:{buf_km}:{hit['level']}"
    st = state.get(key, {'active': False})
    if st.get('active'):
        return True  # già triggerato in precedenza, anti-spam
    state[key] = {'active': True, 'last_trigger_utc': now_iso}

    # moto + probabilità (solo per SRI che ha 2 frame)
    motion, prob = None, None
    if sri_frames and len(sri_frames) >= 2:
        dt_min = abs(sri_frames[0][0] - sri_frames[1][0]) / 60000
        motion = estimate_motion(sri_frames[0][1], sri_frames[1][1], geom, dt_min)
        if motion and signal.get('max_xy_tm'):
            prob = arrival_probability(signal['max_xy_tm'], motion, centroid, buf_km)

    subject, text, md = compose(area, product, hit, signal, motion, prob, buf_km)
    em = send_email(subject, text, to=rcpt_email) if 'email' in channels else 'skipped'
    tg = send_telegram(md, chat_ids=rcpt_tg) if 'telegram' in channels else 'skipped'

    writer.writerow({
        'event_timestamp_utc': now_iso, 'area_name': area['name'],
        'level': f"nowcast_{hit['level']}", 'threshold_mm': hit['value'],
        'observed_mm_mean': f"{signal.get('mean',0):.2f}", 'observed_mm_max': f"{signal['max']:.2f}",
        'product': product, 'observation_timestamp_utc': signal['ts_iso'],
        'forecast_max_6h_mm': '',
        'notified_email': em, 'notified_telegram': tg,
        'note': f"nowcast buffer={buf_km}km" + (f" moto={motion['compass']}/{motion['speed_kmh']}kmh" if motion and motion.get('bearing_deg') is not None else '') + (f" prob={prob}%" if prob is not None else ''),
    })
    log.info(f"  ✓ nowcast {product}/{hit['level']} buf{buf_km}: {signal['max']:.1f} email={em} tg={tg}")
    return True


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if args.dry_run:
        for k in ['SMTP_HOST','SMTP_USER','SMTP_PASS','SMTP_TO','TELEGRAM_TOKEN','TELEGRAM_CHAT_ID']:
            os.environ.pop(k, None)
        log.info('=== DRY-RUN ===')

    script_dir = Path(__file__).resolve().parent
    archive_dir = script_dir.parent
    areas = json.loads((archive_dir / 'areas.json').read_text())['areas']
    enabled = [a for a in areas if a.get('monitoring', {}).get('enabled')]
    if not enabled:
        log.info('Nessuna area attiva.'); return 0

    # ── Scarica i prodotti UNA volta (condivisi tra tutte le aree) ──
    log.info('Download prodotti radar (SRI×2, SRT1, CUM3)…')

    # SRI — 2 frame per stima moto
    sri_times = get_last_products('SRI', n=2)
    sri_frames = []
    for t in sri_times:
        tiff = download_tiff('SRI', t)
        ts_label = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime('%H:%M:%S')
        if tiff:
            sri_frames.append((t, tiff))
            log.info(f'    SRI {ts_label} → OK ({len(tiff):,} bytes)')
        else:
            log.warning(f'    SRI {ts_label} → DOWNLOAD FALLITO')

    # SRT1 — cumulata 1h
    srt1_times = get_last_products('SRT1', n=1)
    srt1_tiff = None
    if srt1_times:
        srt1_bytes = download_tiff('SRT1', srt1_times[0])
        if srt1_bytes:
            srt1_tiff = (srt1_times[0], srt1_bytes)
            log.info(f'    SRT1 → OK ({len(srt1_bytes):,} bytes)')
        else:
            log.warning('    SRT1 → DOWNLOAD FALLITO')

    # CUM3 — cumulata 3h
    cum3_times = get_last_products('CUM3', n=1)
    cum3_tiff = None
    if cum3_times:
        cum3_bytes = download_tiff('CUM3', cum3_times[0])
        if cum3_bytes:
            cum3_tiff = cum3_bytes
            log.info(f'    CUM3 → OK ({len(cum3_bytes):,} bytes)')
        else:
            log.warning('    CUM3 → DOWNLOAD FALLITO')

    # Riepilogo download
    log.info(f'  Riepilogo: SRI frames={len(sri_frames)}/2, '
             f'SRT1={"OK" if srt1_tiff else "MANCANTE"}, '
             f'CUM3={"OK" if cum3_tiff else "MANCANTE"}')

    if not sri_frames:
        log.warning('Nessun frame SRI disponibile, esco.'); return 0

    # ── Elaborazione aree ──
    state_file = archive_dir / 'state' / 'nowcast_state.json'
    state = load_state(state_file)
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

    # Heartbeat: aggiorna sempre il timestamp dell'ultimo run
    state['_last_run_utc'] = now_iso

    # Struttura osservazioni nowcast (verrà mergiata nel file esistente)
    all_obs = {
        '_last_run_utc': now_iso,
        '_sri_frames': len(sri_frames),
        '_srt1_available': srt1_tiff is not None,
        '_cum3_available': cum3_tiff is not None,
    }

    events_file = archive_dir / 'data' / 'events.csv'
    write_header = not events_file.exists()
    with open(events_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_HEADERS)
        if write_header: writer.writeheader()
        for area in enabled:
            log.info(f'[{area["label"]}] nowcast buffer {BUFFERS_KM} km')
            try:
                obs = process_area(area, archive_dir, writer, state, now_iso,
                             sri_frames, srt1_tiff, cum3_tiff)
                all_obs[area['name']] = obs
            except Exception as e:
                log.error(f'  errore: {e}', exc_info=True)
                all_obs[area['name']] = {'error': str(e), 'timestamp_utc': now_iso}

    # ── Salva state e osservazioni ──
    save_state(state_file, state)

    obs_file = archive_dir / 'data' / 'last_observations.json'
    update_nowcast_obs(obs_file, all_obs)

    log.info('Done.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log.error(f'FATAL: {e}', exc_info=True)
        sys.exit(1)
