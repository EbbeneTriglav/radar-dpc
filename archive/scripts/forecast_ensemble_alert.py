#!/usr/bin/env python3
"""
forecast_ensemble_alert.py — Alert pioggia 24h ensemble pesato per Panna.

Gira ogni 60 minuti (workflow). Per l’area Sorgenti Panna:
  1. Scarica forecast 24h da 5 modelli Open-Meteo per 11 punti di controllo
  2. Scarica forecast 24h da MET Norway (validazione indipendente)
  3. Calcola cumulata 24h pesata (pesi idrogeologici per punto)
  4. Calcola media ensemble e worst-case
  5. Confronta con soglie: warning 10mm, alarm 15mm, emergency 20mm
  6. Trigger se worst-case supera soglia E almeno un altro segnale conferma
  7. Notifica email + Telegram, logga in events.csv

Flags speciali:
  --dry-run      esegue tutto ma NON invia notifiche
  --test-alert   invia notifica TEST forecast (dati finti sopra soglia)
  --test-radar   invia notifica TEST radar DPC (simula nowcast trigger)
"""

import argparse
import csv
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests

# ─── Config ──────────────────────────────────────────────────────────────────
OPENMETEO_API = 'https://api.open-meteo.com/v1/forecast'
METNO_API = 'https://api.met.no/weatherapi/locationforecast/2.0/compact'
METNO_UA = 'radar-dpc-forecast/1.0 github.com/ebbenetriglav/radar-dpc'
HTTP_TIMEOUT = 30

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('forecast_24h')

_session = requests.Session()
_session.headers.update({'User-Agent': 'radar-dpc-forecast/1.0', 'Accept': '*/*'})

# ─── Punti di controllo Sorgenti Panna (11 punti, pesi idrogeologici) ───
CONTROL_POINTS = [
    {'id': 'P1',  'name': 'Crinale Nord',      'lat': 44.092,  'lon': 11.283, 'elev': 1180, 'weight': 0.14},
    {'id': 'P2',  'name': 'Versante NW',       'lat': 44.084,  'lon': 11.272, 'elev': 1050, 'weight': 0.12},
    {'id': 'P3',  'name': 'Versante W',        'lat': 44.075,  'lon': 11.278, 'elev':  920, 'weight': 0.11},
    {'id': 'P4',  'name': 'Centro Bacino',     'lat': 44.077,  'lon': 11.299, 'elev':  980, 'weight': 0.13},
    {'id': 'P5',  'name': 'Versante E',        'lat': 44.076,  'lon': 11.320, 'elev':  870, 'weight': 0.11},
    {'id': 'P6',  'name': 'Versante SE',       'lat': 44.065,  'lon': 11.312, 'elev':  780, 'weight': 0.09},
    {'id': 'P7',  'name': 'Fondovalle S',      'lat': 44.061,  'lon': 11.299, 'elev':  640, 'weight': 0.08},
    {'id': 'P8',  'name': 'Ponte a Olmo',      'lat': 44.049,  'lon': 11.304, 'elev':  459, 'weight': 0.05},
    {'id': 'P9',  'name': "Monte di Fo'",      'lat': 44.080,  'lon': 11.281, 'elev':  820, 'weight': 0.06},
    {'id': 'P10', 'name': 'Croce M.Gazzarro',  'lat': 44.0818, 'lon': 11.309, 'elev': 1080, 'weight': 0.06},
    {'id': 'P11', 'name': 'West Gazzarro',     'lat': 44.0872, 'lon': 11.291, 'elev': 1010, 'weight': 0.05},
]

MODELS = [
    {'key': 'icon_seamless',                  'label': 'ICON DWD'},
    {'key': 'ecmwf_ifs025',                   'label': 'IFS ECMWF'},
    {'key': 'gfs_seamless',                   'label': 'GFS NOAA'},
    {'key': 'meteofrance_arpege_europe',      'label': 'ARPEGE MF'},
    {'key': 'meteofrance_arome_france_hd',    'label': 'AROME MF'},
]

THRESHOLDS_24H = [
    {'level': 'warning',   'value_mm': 10, 'icon': '🌧️'},
    {'level': 'alarm',     'value_mm': 15, 'icon': '⛈️'},
    {'level': 'emergency', 'value_mm': 20, 'icon': '⚡'},
]

# ─── Config multi-area: soglie 24h per area ───
# Panna: soglie storiche; Ruspino: matrice 3×3 dashboard, orizzonte 24h
# (Attenzione 30 / Critico 50 / Estremo 80); Cepina: come Ruspino (provvisorio).
AREAS_24H = {
    'panna':   {'thresholds': THRESHOLDS_24H, 'points': 'CONTROL_POINTS'},
    'ruspino': {'thresholds': [
        {'level': 'warning',   'value_mm': 30, 'icon': '🌧️'},
        {'level': 'alarm',     'value_mm': 50, 'icon': '⛈️'},
        {'level': 'emergency', 'value_mm': 80, 'icon': '⚡'},
    ], 'points': 'auto'},
    'cepina':  {'thresholds': [
        {'level': 'warning',   'value_mm': 30, 'icon': '🌧️'},
        {'level': 'alarm',     'value_mm': 50, 'icon': '⛈️'},
        {'level': 'emergency', 'value_mm': 80, 'icon': '⚡'},
    ], 'points': 'auto'},
}


def _area_points(area_name):
    """Punti di controllo: Panna usa gli 11 pesati; altre aree centroide+vertici
    da areas.json con pesi uniformi."""
    if AREAS_24H[area_name]['points'] == 'CONTROL_POINTS':
        return CONTROL_POINTS
    areas_file = Path(__file__).resolve().parents[1] / 'areas.json'
    d = json.loads(areas_file.read_text())
    area = next(a for a in d['areas'] if a['name'] == area_name)
    pts = [{'id': 'C', 'name': 'Centroide', 'lat': area['centroid']['lat'],
            'lon': area['centroid']['lon'], 'elev': 0, 'weight': 1.0}]
    for v in area.get('sample_vertices', []):
        pts.append({'id': v['id'], 'name': v['id'], 'lat': v['lat'],
                    'lon': v['lon'], 'elev': 0, 'weight': 1.0})
    # normalizza pesi
    w = 1.0 / len(pts)
    for pt in pts:
        pt['weight'] = w
    return pts


def _area_recipients(area_name):
    """Recipients per-area da areas.json (fallback None → env default)."""
    try:
        areas_file = Path(__file__).resolve().parents[1] / 'areas.json'
        d = json.loads(areas_file.read_text())
        for a in d['areas']:
            if a['name'] == area_name:
                rcpt = a.get('monitoring', {}).get('recipients', {}) or {}
                return (rcpt.get('email') or None, rcpt.get('telegram_chat_ids') or None)
    except Exception as e:
        log.warning(f'  {area_name} recipients lookup failed: {e}')
    return (None, None)

EVENT_HEADERS = [
    'event_timestamp_utc', 'area_name', 'level', 'threshold_mm',
    'observed_mm_mean', 'observed_mm_max', 'product', 'observation_timestamp_utc',
    'forecast_max_6h_mm', 'notified_email', 'notified_telegram', 'note',
]


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


def fetch_openmeteo_24h(lat, lon):
    """Scarica forecast orario prossime 24h per tutti e 5 i modelli."""
    model_keys = ','.join(m['key'] for m in MODELS)
    try:
        r = _http('GET', OPENMETEO_API, params={
            'latitude': lat, 'longitude': lon,
            'hourly': 'precipitation',
            'forecast_hours': 24,
            'models': model_keys,
            'timezone': 'UTC',
        })
        if not r or not r.ok:
            return None
        data = r.json()
    except Exception as e:
        log.warning(f'  OpenMeteo fetch fallito per {lat},{lon}: {e}')
        return None

    hourly = data.get('hourly', {})
    result = {}
    for model in MODELS:
        key = f"precipitation_{model['key']}"
        values = hourly.get(key, hourly.get('precipitation', []))
        if values:
            total = sum(v for v in values[:24] if v is not None)
            result[model['key']] = round(total, 2)
    return result if result else None


def fetch_metno_24h(lat, lon):
    """Forecast MET Norway: cumulata prossime 24h orarie."""
    try:
        r = _http('GET', METNO_API,
                  params={'lat': round(lat, 4), 'lon': round(lon, 4)},
                  headers={'User-Agent': METNO_UA})
        if not r or not r.ok:
            return None
        ts = r.json().get('properties', {}).get('timeseries', [])
        if not ts:
            return None
        total = 0.0
        for entry in ts[:24]:
            p = entry.get('data', {}).get('next_1_hours', {}).get('details', {}).get('precipitation_amount', 0)
            total += p or 0
        return round(total, 2)
    except Exception as e:
        log.warning(f'  MET Norway fetch fallito: {e}')
        return None


def compute_weighted_ensemble(points=None):
    if points is None:
        points = CONTROL_POINTS
    """Calcola cumulata 24h pesata per mean, worst-case e MET Norway."""
    total_weight = sum(p["weight"] for p in points)
    weighted_mean = 0.0
    weighted_worst = 0.0
    weighted_metno = 0.0
    metno_ok = True
    points_details = []

    for pt in points:
        log.info(f'  [{pt["id"]}] {pt["name"]} ({pt["lat"]}, {pt["lon"]}) w={pt["weight"]}')
        w = pt['weight'] / total_weight

        om = fetch_openmeteo_24h(pt['lat'], pt['lon'])
        if not om:
            log.warning(f'    OpenMeteo fallito, skip punto')
            continue

        model_values = list(om.values())
        pt_mean = sum(model_values) / len(model_values)
        pt_worst = max(model_values)
        weighted_mean += pt_mean * w
        weighted_worst += pt_worst * w

        metno_val = fetch_metno_24h(pt['lat'], pt['lon'])
        if metno_val is not None:
            weighted_metno += metno_val * w
        else:
            metno_ok = False

        log.info(f'    OM mean={pt_mean:.1f} worst={pt_worst:.1f} MET.no={metno_val}')
        points_details.append({
            'id': pt['id'], 'name': pt['name'],
            'om_models': om, 'om_mean': round(pt_mean, 2),
            'om_worst': round(pt_worst, 2), 'metno': metno_val,
        })
        time.sleep(0.3)

    return {
        'mean_ensemble': round(weighted_mean, 2),
        'worst_case': round(weighted_worst, 2),
        'metno_weighted': round(weighted_metno, 2) if metno_ok else None,
        'points': points_details,
        'n_points': len(points_details),
        'n_models': len(MODELS),
    }


def evaluate_24h_thresholds(ensemble, state, now_iso, anti_spam_min=120,
                            area_name='panna', thresholds=None):
    if thresholds is None:
        thresholds = THRESHOLDS_24H
    """
    Trigger se worst_case >= soglia E (mean >= soglia OPPURE metno >= soglia).
    Anti-spam: non ri-notifica finche lo stato e attivo.
    """
    triggers = []
    mean_val = ensemble['mean_ensemble']
    worst_val = ensemble['worst_case']
    metno_val = ensemble.get('metno_weighted')

    for th in sorted(thresholds, key=lambda x: x['value_mm']):
        mm = th['value_mm']
        key = f"{area_name}:forecast_24h:{th['level']}"
        st = state.get(key, {'active': False})

        worst_ok = worst_val >= mm
        mean_ok = mean_val >= mm
        metno_ok = (metno_val is not None and metno_val >= mm)
        confirmed = worst_ok and (mean_ok or metno_ok)

        if confirmed:
            if not st.get('active'):
                state[key] = {'active': True, 'last_trigger_utc': now_iso}
                triggers.append({**th,
                    'mean_val': mean_val, 'worst_val': worst_val, 'metno_val': metno_val})
        else:
            if worst_val < mm * 0.5:
                state[key] = {'active': False}

    return triggers


def send_email(subject, text, html=None, to=None):
    h, port = os.environ.get('SMTP_HOST'), int(os.environ.get('SMTP_PORT', '587'))
    u, pw = os.environ.get('SMTP_USER'), os.environ.get('SMTP_PASS')
    if to:
        if isinstance(to, (list, tuple)):
            to = ','.join(to)
    else:
        to = os.environ.get('SMTP_TO')
    if not (h and u and pw and to):
        log.info('  email: secrets/destinatari mancanti, skip')
        return 'skipped'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'], msg['From'], msg['To'] = subject, u, to
        msg.attach(MIMEText(text, 'plain', 'utf-8'))
        if html: msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP(h, port, timeout=20) as s:
            s.starttls(); s.login(u, pw)
            s.sendmail(u, [x.strip() for x in to.split(',')], msg.as_string())
        log.info(f'  email inviata a {to}')
        return 'true'
    except Exception as e:
        log.warning(f'  email fail: {e}'); return 'false'


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


def send_telegram(md, chat_ids=None):
    tok = os.environ.get('TELEGRAM_TOKEN')
    if chat_ids:
        if isinstance(chat_ids, str):
            chat_ids = [c.strip() for c in chat_ids.split(',') if c.strip()]
    else:
        d = os.environ.get('TELEGRAM_CHAT_ID')
        chat_ids = [d] if d else []
    if not (tok and chat_ids):
        log.info('  telegram: secrets/chat_ids mancanti, skip')
        return 'skipped'
    n_ok = sum(1 for chat in chat_ids if _tg_send_one(tok, chat, md))
    if n_ok:
        log.info('  telegram inviato a %d/%d chat' % (n_ok, len(chat_ids)))
    return 'true' if n_ok else 'false'


def _panna_recipients():
    """Carica recipients di Panna da areas.json. Ritorna (email_list, tg_list)
    o (None, None) se assenti → fallback a env defaults."""
    try:
        import json
        from pathlib import Path
        areas_file = Path(__file__).resolve().parents[1] / 'areas.json'
        d = json.loads(areas_file.read_text())
        for a in d['areas']:
            if a['name'] == 'panna':
                rcpt = a.get('monitoring', {}).get('recipients', {}) or {}
                return (rcpt.get('email') or None, rcpt.get('telegram_chat_ids') or None)
    except Exception as e:
        log.warning(f'  panna recipients lookup failed: {e}')
    return (None, None)


def _build_html(prefix, icon, lvl, color, mean_v, worst_v, metno_str, n_pts, n_mod, mm):
    """Build HTML email body for forecast 24h alert."""
    return (
        '<div style="font-family:Arial,sans-serif;max-width:600px">'
        '<div style="background:' + color + ';color:white;padding:12px 18px;border-radius:6px 6px 0 0">'
        '<h2 style="margin:0">' + prefix + icon + ' ' + area_label + ' \u2014 ' + lvl.upper()
        + ' <span style="font-size:14px;font-weight:normal">(forecast 24h)</span></h2>'
        '</div>'
        '<div style="border:1px solid #ddd;border-top:0;padding:18px;border-radius:0 0 6px 6px">'
        '<p>Ensemble pesato su <b>' + str(n_pts) + ' punti</b> \u00d7 <b>' + str(n_mod) + ' modelli</b>:</p>'
        '<ul>'
        '<li>Media ensemble: <b>' + f'{mean_v:.1f}' + ' mm</b> (soglia ' + str(mm) + ')</li>'
        '<li>Worst-case: <b>' + f'{worst_v:.1f}' + ' mm</b></li>'
        '<li>MET Norway: <b>' + metno_str + '</b></li>'
        '</ul>'
        '<p style="font-size:11px;color:#888">Open-Meteo + MET Norway \u2022 prossime 24 ore</p>'
        '</div></div>'
    )


def compose_24h(trigger, ensemble, prefix="", area_label='Sorgenti Panna'):
    lvl = trigger['level']
    icon = trigger['icon']
    mm = trigger['value_mm']
    mean_v = trigger['mean_val']
    worst_v = trigger['worst_val']
    metno_v = trigger.get('metno_val')
    metno_str = f'{metno_v:.1f} mm' if metno_v is not None else 'N/D'
    n_pts = ensemble['n_points']
    n_mod = ensemble['n_models']

    pt0 = ensemble['points'][0] if ensemble['points'] else {}
    models_str = ''
    if pt0.get('om_models'):
        for mk, mv in pt0['om_models'].items():
            label = next((m['label'] for m in MODELS if m['key'] == mk), mk)
            models_str += f'  \u2022 {label}: {mv:.1f} mm\n'

    text = (
        f'{prefix}{icon} PREVISIONE 24H \u2014 {area_label} \u2014 {lvl.upper()}\n\n'
        f'Ensemble pesato su {n_pts} punti \u00d7 {n_mod} modelli:\n'
        f'  \u2022 Media ensemble: {mean_v:.1f} mm (soglia {mm} mm)\n'
        f'  \u2022 Worst-case:     {worst_v:.1f} mm\n'
        f'  \u2022 MET Norway:      {metno_str}\n\n'
        f'Dettaglio modelli (punto {pt0.get("name", "?")}):\n'
        f'{models_str}\n'
        f'Soglia superata: {mm} mm/24h\n'
    )

    md = (
        f'{prefix}{icon} *PREVISIONE 24H \u2014 Panna*\n'
        f'Livello: *{lvl.upper()}* (soglia {mm} mm)\n\n'
        f'Media ensemble: *{mean_v:.1f} mm*\n'
        f'Worst-case: *{worst_v:.1f} mm*\n'
        f'MET Norway: *{metno_str}*\n\n'
        f'_{n_pts} punti \u00d7 {n_mod} modelli_'
    )

    color = {"warning": "#e0a800", "alarm": "#e85e2c", "emergency": "#c41e3a"}.get(lvl, "#888")
    html = _build_html(prefix, icon, lvl, color, mean_v, worst_v, metno_str, n_pts, n_mod, mm)

    subject = f'{prefix}{icon} {area_label} \u2014 FORECAST 24H {lvl.upper()} ({worst_v:.1f} mm worst-case)'
    return subject, text, html, md


def load_state(f):
    if f.exists():
        try: return json.loads(f.read_text())
        except Exception: return {}
    return {}

def save_state(f, st):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=2, sort_keys=True))


def update_observations(file, ensemble, now_iso, area_name='panna'):
    """Mergia forecast_24h in last_observations.json."""
    data = {}
    if file.exists():
        try: data = json.loads(file.read_text())
        except Exception: data = {}

    data['_forecast24h_last_run_utc'] = now_iso
    data.setdefault(area_name, {})
    data[area_name]['forecast_24h'] = {
        'mean_ensemble': ensemble['mean_ensemble'],
        'worst_case': ensemble['worst_case'],
        'metno_weighted': ensemble.get('metno_weighted'),
        'n_points': ensemble['n_points'],
        'n_models': ensemble['n_models'],
        'updated_at_utc': now_iso,
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    log.info(f'  last_observations.json aggiornato (forecast_24h)')


def run_test_alert():
    """Invia notifica TEST forecast con dati finti sopra soglia."""
    log.info('=== TEST ALERT FORECAST 24H ===')
    fake_trigger = {
        'level': 'warning', 'value_mm': 10, 'icon': '🌧️',
        'mean_val': 12.5, 'worst_val': 18.3, 'metno_val': 11.8,
    }
    fake_ensemble = {
        'mean_ensemble': 12.5, 'worst_case': 18.3, 'metno_weighted': 11.8,
        'n_points': 11, 'n_models': 5,
        'points': [{'id': 'P1', 'name': 'Crinale Nord (TEST)',
            'om_models': {'icon_seamless': 15.2, 'ecmwf_ifs025': 11.3,
                'gfs_seamless': 10.8, 'meteofrance_arpege_europe': 12.1,
                'meteofrance_arome_france_hd': 18.3},
            'om_mean': 13.5, 'om_worst': 18.3, 'metno': 11.8}],
    }
    subject, text, html, md = compose_24h(fake_trigger, fake_ensemble, prefix='[TEST] ')
    rcpt_email, rcpt_tg = _panna_recipients()
    em = send_email(subject, text, html, to=rcpt_email)
    tg = send_telegram(md, chat_ids=rcpt_tg)
    log.info(f'TEST alert inviato: email={em} telegram={tg}')
    log.info('Se hai ricevuto email + Telegram con [TEST], il sistema funziona!')
    return 0


def run_test_radar():
    """Invia notifica TEST simulando un alert radar DPC."""
    log.info('=== TEST ALERT RADAR DPC (NOWCAST) ===')
    subject = "[TEST] ⛈️ Panna — CELLA RADAR WARNING (SIMULATA)"
    text = (
        '[TEST] ⛈️ CELLA RADAR IN AVVICINAMENTO — Panna — WARNING\n\n'
        'Questo è un TEST per verificare che le notifiche radar funzionino.\n\n'
        'Dati simulati (NON REALI):\n'
        '  • SRI max: 12.5 mm/h nel buffer 10km (soglia 10)\n'
        '  • Cumulata 3h: 8.3 mm\n'
        '  • Moto: verso NE a 15.2 km/h\n'
        '  • Probabilità arrivo: 72%\n\n'
        'Se vedi questo messaggio, il sistema radar è configurato correttamente!\n'
    )
    md = (
        '[TEST] ⛈️ *CELLA RADAR — Panna*\n'
        'Livello: *WARNING* (TEST)\n\n'
        'SRI max: *12.5 mm/h* (buf 10km)\n'
        'Moto: NE a 15.2 km/h\n'
        'Prob. arrivo: *72%*\n\n'
        '_Questo è un TEST — dati non reali_'
    )
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:600px">'
        '<div style="background:#e0a800;color:white;padding:12px 18px;border-radius:6px 6px 0 0">'
        '<h2 style="margin:0">[TEST] ⛈️ Panna — WARNING (radar simulato)</h2>'
        '</div>'
        '<div style="border:1px solid #ddd;border-top:0;padding:18px;border-radius:0 0 6px 6px">'
        '<p><b>Questo è un TEST</b> per verificare le notifiche radar.</p>'
        '<ul>'
        '<li>SRI max: <b>12.5 mm/h</b> (buffer 10km)</li>'
        '<li>Cumulata 3h: <b>8.3 mm</b></li>'
        '<li>Moto: verso <b>NE</b> a 15.2 km/h</li>'
        '<li>Probabilità arrivo: <b>72%</b></li>'
        '</ul>'
        '<p style="font-size:11px;color:#888">Dati simulati — non reali</p>'
        '</div></div>'
    )
    rcpt_email, rcpt_tg = _panna_recipients()
    em = send_email(subject, text, html, to=rcpt_email)
    tg = send_telegram(md, chat_ids=rcpt_tg)
    log.info(f'TEST radar inviato: email={em} telegram={tg}')
    log.info('Se hai ricevuto email + Telegram con [TEST] radar, il sistema funziona!')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true', help='No notifiche')
    ap.add_argument('--test-alert', action='store_true', help='Invia TEST forecast')
    ap.add_argument('--test-radar', action='store_true', help='Invia TEST radar DPC')
    args = ap.parse_args()

    if args.dry_run:
        for k in ['SMTP_HOST','SMTP_USER','SMTP_PASS','SMTP_TO','TELEGRAM_TOKEN','TELEGRAM_CHAT_ID']:
            os.environ.pop(k, None)
        log.info('=== DRY-RUN ===')

    if args.test_alert:
        return run_test_alert()
    if args.test_radar:
        return run_test_radar()

    script_dir = Path(__file__).resolve().parent
    archive_dir = script_dir.parent
    state_file = archive_dir / 'state' / 'forecast24h_state.json'
    obs_file = archive_dir / 'data' / 'last_observations.json'
    events_file = archive_dir / 'data' / 'events.csv'

    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

    state = load_state(state_file)
    state['_last_run_utc'] = now_iso

    # Labels per i messaggi
    AREA_LABELS = {'panna': 'Sorgenti Panna', 'ruspino': 'Ruspino', 'cepina': 'Cepina'}

    write_header = not events_file.exists()
    with open(events_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_HEADERS)
        if write_header: writer.writeheader()

        for area_name, cfg in AREAS_24H.items():
            label = AREA_LABELS.get(area_name, area_name)
            try:
                points = _area_points(area_name)
            except Exception as e:
                log.warning(f'[{label}] punti non disponibili: {e}'); continue

            log.info(f'[{label}] ensemble {len(points)} punti \u00d7 5 modelli + MET Norway...')
            try:
                ensemble = compute_weighted_ensemble(points)
            except Exception as e:
                log.warning(f'[{label}] ensemble fallito: {e}'); continue

            log.info(f'  mean={ensemble["mean_ensemble"]:.1f} worst={ensemble["worst_case"]:.1f} '
                     f'metno={ensemble.get("metno_weighted","N/D")} punti_ok={ensemble["n_points"]}/{len(points)}')

            update_observations(obs_file, ensemble, now_iso, area_name=area_name)

            triggers = evaluate_24h_thresholds(ensemble, state, now_iso,
                                               area_name=area_name,
                                               thresholds=cfg['thresholds'])
            if not triggers:
                log.info('  nessuna soglia 24h superata')
                continue

            rcpt_email, rcpt_tg = _area_recipients(area_name)
            for tr in triggers:
                subject, text, html, md = compose_24h(tr, ensemble, area_label=label)
                em = send_email(subject, text, html, to=rcpt_email)
                tg = send_telegram(md, chat_ids=rcpt_tg)
                writer.writerow({
                    'event_timestamp_utc': now_iso,
                    'area_name': area_name,
                    'level': f"forecast24h_{tr['level']}",
                    'threshold_mm': tr['value_mm'],
                    'observed_mm_mean': f"{ensemble['mean_ensemble']:.2f}",
                    'observed_mm_max': f"{ensemble['worst_case']:.2f}",
                    'product': 'ensemble_24h',
                    'observation_timestamp_utc': '',
                    'forecast_max_6h_mm': f"{ensemble['worst_case']:.2f}",
                    'notified_email': em,
                    'notified_telegram': tg,
                    'note': f"mean={ensemble['mean_ensemble']:.1f} worst={ensemble['worst_case']:.1f} metno={ensemble.get('metno_weighted','N/D')}",
                })
                log.info(f"  \u2713 trigger {tr['level']}: email={em} tg={tg}")

    save_state(state_file, state)
    log.info('Done.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log.error(f'FATAL: {e}', exc_info=True)
        sys.exit(1)
