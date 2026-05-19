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
from shapely.geometry import Polygon, mapping

# ─── Config & logging ────────────────────────────────────────────────────────

DPC_API = 'https://radar-api.protezionecivile.it'
OPENMETEO_API = 'https://api.open-meteo.com/v1/forecast'
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

def fetch_forecast(lat, lon, hours=6):
    """
    Restituisce max precipitazione attesa nei prossimi N ore (mm/15min totali).
    Granularità 15 minuti.
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
        # somma rolling 1h (4 step da 15 min) per stimare max cumulata 1h prevista
        max_1h = 0.0
        for i in range(len(prec) - 3):
            window = sum(p for p in prec[i:i+4] if p is not None)
            if window > max_1h:
                max_1h = window
        return {
            'max_1h_next': round(max_1h, 2),
            'total_period': round(sum(p or 0 for p in prec), 2),
            'horizon_hours': hours,
        }
    except Exception as e:
        log.warning(f'  forecast fallito: {e}')
        return None


# ─── Notifiche: Email + Telegram ─────────────────────────────────────────────

def send_email(subject, body_text, body_html=None):
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


def send_telegram(text_markdown):
    token = os.environ.get('TELEGRAM_TOKEN')
    chat  = os.environ.get('TELEGRAM_CHAT_ID')
    if not (token and chat):
        log.info('  telegram: secrets mancanti, skip')
        return 'skipped'
    try:
        r = _http('POST', f'https://api.telegram.org/bot{token}/sendMessage', json={
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


def _level_key(area_name, level):
    return f'{area_name}:{level}'


def evaluate_thresholds(area, current_value, state, now_iso, anti_spam_min, rearm_pct):
    """
    Per ogni soglia decide se TRIGGERARE (nuova notifica) o RIARMARE.
    Ritorna: lista di soglie da notificare (dict).
    Modifica state in place.
    """
    triggers = []
    thresholds = sorted(area['monitoring']['thresholds'],
                        key=lambda x: x['value_mm'])

    for th in thresholds:
        key = _level_key(area['name'], th['level'])
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

def compose_messages(area, threshold, stats, observation_ts_iso, forecast):
    label = area['label']
    lvl   = threshold['level']
    icon  = threshold['icon']
    val_mm = threshold['value_mm']
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

    # Plaintext
    text = (
        f"{icon} ALLERTA PIOGGIA — {label} — livello {lvl.upper()}\n\n"
        f"Cumulata oraria osservata (SRT1):\n"
        f"  • Media area: {obs_mean:.1f} mm\n"
        f"  • Max area:   {obs_max:.1f} mm\n"
        f"Soglia superata: {val_mm} mm\n"
        f"Ora osservazione: {obs_str}\n\n"
        f"{fc_line}"
        f"Dati: API Protezione Civile — radar-api.protezionecivile.it\n"
    )

    # Telegram markdown
    md = (
        f"{icon} *ALLERTA — {label}*\n"
        f"Livello: *{lvl.upper()}*\n"
        f"Soglia: {val_mm} mm/1h\n\n"
        f"Osservato:\n"
        f"  media: *{obs_mean:.1f} mm*\n"
        f"  max:   *{obs_max:.1f} mm*\n"
        f"_{obs_str}_\n"
    )
    if forecast:
        md += f"\nForecast {forecast['horizon_hours']}h: max 1h *{forecast['max_1h_next']:.1f} mm*"

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
        <p style="font-size:11px;color:#888">Dati: API Protezione Civile</p>
      </div>
    </div>
    """

    subject = f"{icon} {label} — {lvl.upper()} (cumulata 1h: {obs_mean:.1f} mm)"
    return subject, text, html, md


# ─── Pipeline ────────────────────────────────────────────────────────────────

def process_area(area, archive_dir, events_writer):
    if not area.get('monitoring', {}).get('enabled'):
        return

    mon = area['monitoring']
    product = mon.get('product', 'SRT1')
    metric  = mon.get('metric', 'mean')   # 'mean' o 'max'
    anti_spam = int(mon.get('anti_spam_minutes', 30))
    rearm_pct = int(mon.get('rearm_below_pct', 50))

    log.info(f'[{area["label"]}] monitoring {product} ({metric})')

    # 1) ultimo timestamp disponibile
    last = get_last_product(product)
    if not last:
        log.warning(f'  no last product for {product}')
        return
    ts_ms = last['time']
    ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    ts_iso = ts_dt.isoformat().replace('+00:00', 'Z')

    # 2) download GeoTIFF
    url = get_pre_signed_url(product, ts_ms)
    if not url:
        return
    tiff = download_geotiff(url)
    if not tiff:
        return

    # 3) stats sul poligono
    stats = stats_for_polygon(tiff, area['polygon'])
    if not stats:
        log.warning(f'  stats N/D')
        return

    metric_value = stats[metric]
    log.info(f'  observation {ts_iso}: mean={stats["mean"]:.2f} max={stats["max"]:.2f} mm')

    # 4) valuta soglie
    state_file = archive_dir / 'state' / 'monitor_state.json'
    state = load_state(state_file)
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    triggers = evaluate_thresholds(area, metric_value, state, now_iso, anti_spam, rearm_pct)
    save_state(state_file, state)

    if not triggers:
        log.info('  nessuna soglia attivata')
        return

    # 5) forecast (una volta sola, condiviso fra tutti i trigger)
    forecast = None
    if mon.get('forecast', {}).get('enabled'):
        forecast = fetch_forecast(
            area['centroid']['lat'],
            area['centroid']['lon'],
            hours=mon['forecast'].get('lookahead_hours', 6),
        )
        if forecast:
            log.info(f'  forecast max 1h next 6h: {forecast["max_1h_next"]:.1f} mm')

    # 6) invia notifiche per ciascun trigger
    channels = set(mon.get('channels', []))
    for th in triggers:
        subject, text, html, md = compose_messages(area, th, stats, ts_iso, forecast)
        email_status = send_email(subject, text, html) if 'email' in channels else 'skipped'
        tg_status    = send_telegram(md) if 'telegram' in channels else 'skipped'

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
            'note':                       '',
        })
        log.info(f'  ✓ trigger {th["level"]}: email={email_status} telegram={tg_status}')


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
