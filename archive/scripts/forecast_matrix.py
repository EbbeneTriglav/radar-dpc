#!/usr/bin/env python3
"""
forecast_matrix.py — Matrice rischio scarico preventivo previsionale (24/48/72h) via email.

Replica ESATTAMENTE gli ingredienti delle dashboard:
  · Ruspino v2.0: 11 punti pesati (POINTS_RUSPINO, copiati verbatim dalla
    dashboard), 5 modelli Open-Meteo, MET Norway come validazione SEPARATA
    (mai mediato nell'ensemble), cumulate MOBILI 24/48/72h, worst-case =
    modello più piovoso, matrice 3×2 a soglia unica di scarico:
      Attenzione [60, 80, 100] · Critico [80, 120, 140] (24/48/72h)
      Scarico preventivo al livello CRITICO in QUALSIASI orizzonte.
      Prossimità: warning se worst ≥ 85% della soglia successiva (proxFactor).
  · Panna v6.6: 11 punti CONTROL_POINTS (già identici alla dashboard),
    criterio B su CUMULATA GIORNALIERA worst-case (giorni civili Europe/Rome):
      Attenzione 5 · Critico 10 · Estremo 15 mm/giorno.
    Orizzonti = Giorno 1 / 2 / 3.

Invio: SOLO EMAIL ai destinatari dell'area (per scelta esplicita la matrice
NON va su Telegram, NON viene loggata in events.csv e NON compare sulle
pagine pubbliche — il repo è pubblico, quindi qualunque log lo sarebbe).
Lo stato anti-spam (state/forecast_matrix_state.json) contiene solo
data+livello dell'ultimo invio.

Cadenza invio: al massimo 1 email/giorno/area quando una soglia è superata;
re-invio nello stesso giorno SOLO se il livello peggiora (escalation).

Limiti dichiarati (nessun numero inventato):
  · MET Norway fornisce dati orari (~60h) poi 6-orari: la validazione MET
    copre solo la parte oraria; la copertura effettiva è indicata in email.
  · Se un modello/punto non risponde, il calcolo prosegue con quelli
    disponibili e il conteggio n/5 modelli e punti OK è indicato in email.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Riuso ingredienti esistenti (stessa directory)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forecast_ensemble_alert import (  # noqa: E402
    CONTROL_POINTS, MODELS, METNO_API, METNO_UA, OPENMETEO_API,
    _http, send_email, send_telegram, _area_recipients,
)

log = logging.getLogger('forecast_matrix')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')

TZ_ROME = ZoneInfo('Europe/Rome')
FORECAST_HOURS = 72

# ── Punti di controllo Ruspino — COPIATI VERBATIM dalla dashboard v2.0 ──
POINTS_RUSPINO = [
    {'id': 'P1',  'name': 'Sornadello',    'lat': 45.8672, 'lon': 9.6505, 'elev': 1450, 'weight': 0.11},
    {'id': 'P2',  'name': 'M. Foldone',    'lat': 45.8623, 'lon': 9.6373, 'elev': 1350, 'weight': 0.10},
    {'id': 'P3',  'name': 'Pizzo Grande',  'lat': 45.8574, 'lon': 9.6439, 'elev': 1250, 'weight': 0.10},
    {'id': 'P4',  'name': 'Castel Regina', 'lat': 45.8524, 'lon': 9.6176, 'elev': 1300, 'weight': 0.10},
    {'id': 'P5',  'name': "Ca' Boffelli",  'lat': 45.8464, 'lon': 9.6353, 'elev': 1000, 'weight': 0.12},
    {'id': 'P6',  'name': 'Piazzacava',    'lat': 45.8475, 'lon': 9.6571, 'elev':  780, 'weight': 0.09},
    {'id': 'P7',  'name': 'M. Molinasco',  'lat': 45.8377, 'lon': 9.6242, 'elev': 1050, 'weight': 0.09},
    {'id': 'P8',  'name': 'Sussia',        'lat': 45.8426, 'lon': 9.6505, 'elev':  850, 'weight': 0.08},
    {'id': 'P9',  'name': 'Val Grande',    'lat': 45.8278, 'lon': 9.6307, 'elev':  720, 'weight': 0.07},
    {'id': 'P10', 'name': 'Ronco',         'lat': 45.8327, 'lon': 9.6373, 'elev':  900, 'weight': 0.07},
    {'id': 'P11', 'name': 'Cornalita',     'lat': 45.8475, 'lon': 9.6637, 'elev':  650, 'weight': 0.07},
]

# ── Punti di controllo Cepina — 7 sorgenti del bacino, estratte
#    dalla dashboard analitica Sorgenti_Oga.html. Pesi UNIFORMI (1/7): non
#    esiste ancora una pesatura idrogeologica validata per questo bacino (a
#    differenza di Ruspino/Panna). Dichiarato, non inventato: quando avrai i
#    pesi reali basta aggiornare 'weight'. Criterio soglie = giornaliero Panna
#    (5/10/15 mm/g worst-case), provvisorio finché non tari soglie proprie.
_CEPINA_PTS_RAW = [
    ('Sorgente 1', 46.4393, 10.3344, 1780),
    ('Sorgente 2', 46.4318, 10.3379, 1750),
    ('Sorgente 3', 46.4330, 10.3455, 1550),
    ('Sorgente 4', 46.4215, 10.3436, 1540),
    ('Sorgente 5', 46.4378, 10.3480, 1400),
    ('Sorgente 6', 46.4360, 10.3400, 1400),
    ('Sorgente 7', 46.4300, 10.3440, 1350),
]
POINTS_CEPINA = [
    {'id': f'C{i+1}', 'name': n, 'lat': la, 'lon': lo, 'elev': el, 'weight': 1.0/len(_CEPINA_PTS_RAW)}
    for i, (n, la, lo, el) in enumerate(_CEPINA_PTS_RAW)
]
MATRIX2 = {
    'horizons': [
        {'key': '1g', 'h': 24, 'label': '1 giorno', 'sub': '24h'},
        {'key': '2g', 'h': 48, 'label': '2 giorni', 'sub': '48h'},
        {'key': '3g', 'h': 72, 'label': '3 giorni', 'sub': '72h'},
    ],
    'levels': ['Attenzione', 'Critico'],
    'colors': ['#0ea5e9', '#dc2626'],
    'thr': {'1g': [60, 80], '2g': [80, 120], '3g': [100, 140]},
    'sp3_min_level': 1,      # scarico al Critico, qualsiasi orizzonte
    'prox_factor': 0.85,     # prossimità: worst ≥ 85% soglia successiva
}

# ── Soglie giornaliere Panna — IDENTICHE alla dashboard v6.6 (thr) ──
PANNA_DAY_THR = {'att': 5, 'crit': 10, 'ext': 15}   # mm/giorno, worst-case
PANNA_LEVELS = ['Attenzione', 'Critico', 'Estremo']
PANNA_COLORS = ['#f59e0b', '#ef4444', '#7c3aed']

LEVEL_RANK = {'': 0, 'Attenzione': 1, 'Critico': 2, 'Estremo': 3}


# ─── Fetch serie orarie ──────────────────────────────────────────────────────

def fetch_om_hourly_72(lat, lon):
    """Serie orarie 72h di precipitazione per i 5 modelli. → {model_key: [mm]*}"""
    model_keys = ','.join(m['key'] for m in MODELS)
    r = _http('GET', OPENMETEO_API, params={
        'latitude': lat, 'longitude': lon,
        'hourly': 'precipitation',
        'forecast_hours': FORECAST_HOURS,
        'models': model_keys,
        'timezone': 'UTC',
    })
    if not r or not r.ok:
        return None, None
    data = r.json()
    hourly = data.get('hourly', {})
    times = hourly.get('time', [])
    out = {}
    for m in MODELS:
        vals = hourly.get(f"precipitation_{m['key']}", hourly.get('precipitation'))
        if vals:
            out[m['key']] = [v if v is not None else 0.0 for v in vals[:FORECAST_HOURS]]
    return (out or None), times[:FORECAST_HOURS]


def fetch_metno_hourly(lat, lon):
    """Serie oraria MET Norway (solo next_1_hours: niente valori inventati
    dalla parte 6-oraria). → lista mm, lunghezza = copertura oraria reale."""
    r = _http('GET', METNO_API,
              params={'lat': round(lat, 4), 'lon': round(lon, 4)},
              headers={'User-Agent': METNO_UA})
    if not r or not r.ok:
        return None
    ts = r.json().get('properties', {}).get('timeseries', [])
    out = []
    for entry in ts:
        d = entry.get('data', {}).get('next_1_hours')
        if d is None:
            break   # finita la parte oraria: stop, non interpolare
        out.append(d.get('details', {}).get('precipitation_amount', 0) or 0)
        if len(out) >= FORECAST_HOURS:
            break
    return out or None


# ─── Aggregazione pesata (stessi ingredienti dashboard) ─────────────────────

def weighted_series_by_model(points):
    """Per ogni modello: serie oraria pesata sui punti. Inoltre MET.no pesato.
    Ritorna (series_by_model, metno_series, n_points_ok, metno_cov_h, times0)."""
    per_model = {m['key']: [0.0] * FORECAST_HOURS for m in MODELS}
    model_weight_sum = {m['key']: 0.0 for m in MODELS}
    metno_acc, metno_wsum, metno_cov = [0.0] * FORECAST_HOURS, 0.0, FORECAST_HOURS
    n_ok, times0 = 0, None

    for pt in points:
        w = pt['weight']
        om, times = fetch_om_hourly_72(pt['lat'], pt['lon'])
        if om:
            n_ok += 1
            times0 = times0 or times
            for mk, vals in om.items():
                for i, v in enumerate(vals):
                    per_model[mk][i] += v * w
                model_weight_sum[mk] += w
        mn = fetch_metno_hourly(pt['lat'], pt['lon'])
        if mn:
            metno_cov = min(metno_cov, len(mn))
            for i, v in enumerate(mn[:FORECAST_HOURS]):
                metno_acc[i] += v * w
            metno_wsum += w
        log.info(f"    {pt['id']} {pt['name']}: OM={'ok' if om else 'N/D'} "
                 f"MET={len(mn) if mn else 0}h")

    series = {}
    for mk in per_model:
        ws = model_weight_sum[mk]
        if ws > 0:
            series[mk] = [v / ws for v in per_model[mk]]
    metno = None
    if metno_wsum > 0:
        metno = [v / metno_wsum for v in metno_acc[:metno_cov]]
    return series, metno, n_ok, (metno_cov if metno else 0), times0


def max_rolling_sum(series, window_h):
    """Massima somma mobile su finestra window_h (equivalente maxRollingSum
    della dashboard). Se la serie è più corta della finestra, usa la somma
    totale disponibile (copertura parziale, da dichiarare)."""
    n = len(series)
    if n == 0:
        return 0.0
    if n <= window_h:
        return round(sum(series), 1)
    s = sum(series[:window_h])
    best = s
    for i in range(window_h, n):
        s += series[i] - series[i - window_h]
        if s > best:
            best = s
    return round(best, 1)


# ─── Ruspino: matrice 3×2 su cumulate mobili ────────────────────────────────

def compute_ruspino_matrix():
    series, metno, n_pts, metno_cov, _ = weighted_series_by_model(POINTS_RUSPINO)
    if not series:
        return None
    n_models = len(series)
    # media ensemble oraria (solo modelli disponibili — MET.no MAI incluso)
    ens_mean = [sum(series[mk][i] for mk in series) / n_models
                for i in range(FORECAST_HOURS)]

    rows = []
    for hz in MATRIX2['horizons']:
        H = hz['h']
        per_model = {mk: max_rolling_sum(series[mk], H) for mk in series}
        worst_key = max(per_model, key=per_model.get)
        worst = per_model[worst_key]
        worst_label = next(m['label'] for m in MODELS if m['key'] == worst_key)
        mean_v = max_rolling_sum(ens_mean, H)
        metno_v = max_rolling_sum(metno, H) if metno else None
        thr = MATRIX2['thr'][hz['key']]
        level_idx = 1 if worst >= thr[1] else (0 if worst >= thr[0] else -1)
        # concordanza sulla soglia Attenzione (stesso criterio dashboard)
        n_agree = sum(1 for v in per_model.values() if v >= thr[0])
        # prossimità alla soglia successiva (entro −15%)
        next_idx = level_idx + 1
        prox = None
        if next_idx < len(thr) and worst >= thr[next_idx] * MATRIX2['prox_factor'] and worst < thr[next_idx]:
            prox = round((1 - worst / thr[next_idx]) * 100)
        rows.append({
            'hz': hz, 'thr': thr, 'worst': worst, 'worst_model': worst_label,
            'mean': mean_v, 'metno': metno_v, 'level_idx': level_idx,
            'n_agree': n_agree, 'n_models': n_models, 'prox_pct': prox,
        })
    sp3 = any(r['level_idx'] >= MATRIX2['sp3_min_level'] for r in rows)
    max_level = max((r['level_idx'] for r in rows), default=-1)
    return {'rows': rows, 'sp3': sp3, 'max_level': max_level,
            'n_points_ok': n_pts, 'n_models': n_models, 'metno_cov_h': metno_cov}


# ─── Panna: cumulate giornaliere worst-case (criterio B dashboard) ──────────

def compute_days_worstcase(points):
    """Cumulate giornaliere worst-case (criterio B dashboard Panna). Riusabile
    per qualsiasi bacino con criterio giornaliero (Panna, Cepina)."""
    series, metno, n_pts, metno_cov, times = weighted_series_by_model(points)
    if not series or not times:
        return None
    n_models = len(series)
    # Raggruppa le ore per giorno civile Europe/Rome
    day_keys, day_idx = [], {}
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc).astimezone(TZ_ROME)
        k = dt.date().isoformat()
        if k not in day_idx:
            day_idx[k] = []
            day_keys.append(k)
        day_idx[k].append(i)

    days = []
    for k in day_keys[:3]:
        idxs = day_idx[k]
        per_model = {mk: round(sum(series[mk][i] for i in idxs), 1) for mk in series}
        worst_key = max(per_model, key=per_model.get)
        worst = per_model[worst_key]
        worst_label = next(m['label'] for m in MODELS if m['key'] == worst_key)
        mean_v = round(sum(per_model.values()) / n_models, 1)
        metno_v = round(sum(metno[i] for i in idxs if i < len(metno)), 1) if metno else None
        thr = PANNA_DAY_THR
        level_idx = 2 if worst >= thr['ext'] else 1 if worst >= thr['crit'] else 0 if worst >= thr['att'] else -1
        n_agree = sum(1 for v in per_model.values() if v >= thr['att'])
        n_hours = len(idxs)
        days.append({'date': k, 'worst': worst, 'worst_model': worst_label,
                     'mean': mean_v, 'metno': metno_v, 'level_idx': level_idx,
                     'n_agree': n_agree, 'n_models': n_models, 'n_hours': n_hours})
    max_level = max((d['level_idx'] for d in days), default=-1)
    return {'days': days, 'max_level': max_level, 'n_points_ok': n_pts,
            'n_models': n_models, 'metno_cov_h': metno_cov}


# ─── Email HTML (matrice nascosta: solo destinatari email) ──────────────────

def _cell(v, active, color):
    style = 'padding:8px 12px;border:1px solid #e2e8f0;text-align:center;font-size:13px;'
    if active:
        style += f'background:{color};color:#fff;font-weight:700;'
    return f'<td style="{style}">{v}</td>'


def compose_email_ruspino(res, now_iso):
    lvl_name = MATRIX2['levels'][res['max_level']] if res['max_level'] >= 0 else 'sotto soglia'
    icon = '⛔' if res['sp3'] else ('⚠️' if res['max_level'] >= 0 else '✓')
    subject = (f"{icon} Matrice scarico preventivo Ruspino — {lvl_name}"
               + (' · SCARICO CONSIGLIATO' if res['sp3'] else ''))

    head = ''.join(f'<th style="padding:8px 12px;border:1px solid #e2e8f0;color:{c};font-size:12px">{lv}'
                   + (' · scarico' if i == 1 else '') + '</th>'
                   for i, (lv, c) in enumerate(zip(MATRIX2['levels'], MATRIX2['colors'])))
    body_rows, text_rows = [], []
    for r in res['rows']:
        hz, thr = r['hz'], r['thr']
        cells = ''
        for li in range(2):
            label = f"≥{thr[li]} mm"
            active = r['level_idx'] == li
            cells += _cell(label + (f"<br><b>{r['worst']:.0f} mm</b>" if active else ''),
                           active, MATRIX2['colors'][li])
        prox = (f" · ⚠️ −{r['prox_pct']}% da {MATRIX2['levels'][r['level_idx']+1]}"
                if r['prox_pct'] is not None else '')
        metno_s = f"{r['metno']:.0f} mm" if r['metno'] is not None else 'N/D'
        detail = (f"worst <b>{r['worst']:.0f} mm</b> ({r['worst_model']}) · "
                  f"media ens. {r['mean']:.0f} mm · MET.no {metno_s} · "
                  f"concordanza {r['n_agree']}/{r['n_models']} modelli{prox}")
        body_rows.append(
            f'<tr><td style="padding:8px 12px;border:1px solid #e2e8f0;font-weight:600">'
            f'{hz["label"]} <span style="color:#94a3b8;font-size:11px">{hz["sub"]}</span></td>'
            f'{cells}<td style="padding:8px 12px;border:1px solid #e2e8f0;font-size:12px;color:#334155">{detail}</td></tr>')
        lvn = MATRIX2['levels'][r['level_idx']] if r['level_idx'] >= 0 else 'sotto soglia'
        text_rows.append(f"  {hz['label']} ({hz['sub']}): worst {r['worst']:.0f} mm ({r['worst_model']}) "
                         f"→ {lvn} · media {r['mean']:.0f} · MET.no {metno_s} · "
                         f"concordanza {r['n_agree']}/{r['n_models']}{prox}")

    banner = ''
    if res['sp3']:
        banner = ('<div style="background:#fef2f2;border:2px solid #dc2626;border-radius:8px;'
                  'padding:12px 16px;margin:0 0 14px;font-size:15px;font-weight:700;color:#7f1d1d">'
                  '⛔ SCARICO PREVENTIVO consigliato — livello Critico (worst-case) raggiunto</div>')

    metno_note = (f"MET Norway: copertura oraria {res['metno_cov_h']}/{FORECAST_HOURS}h "
                  "(validazione indipendente, mai mediato nell'ensemble)")
    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#0f172a">
<h2 style="margin:0 0 4px">🚱 Matrice rischio scarico preventivo — Bacino Ruspino</h2>
<p style="color:#64748b;font-size:12px;margin:0 0 14px">Cumulate mobili 24/48/72h · worst-case = modello più piovoso ·
soglie identiche alla dashboard v2.0 (matrice 3×2, soglia unica di scarico) · run {now_iso}</p>
{banner}
<table style="border-collapse:collapse">
<tr><th style="padding:8px 12px;border:1px solid #e2e8f0"></th>{head}
<th style="padding:8px 12px;border:1px solid #e2e8f0;font-size:12px;color:#64748b">Dettaglio quantitativo</th></tr>
{''.join(body_rows)}
</table>
<p style="font-size:11px;color:#94a3b8;margin:14px 0 0">
Ensemble: {res['n_models']}/5 modelli Open-Meteo (ICON DWD · IFS ECMWF · GFS NOAA · ARPEGE MF · AROME MF),
11 punti pesati ({res['n_points_ok']}/11 OK) · {metno_note}.<br>
Cella evidenziata = livello del worst-case. Contenuto riservato ai destinatari email
(non pubblicato su dashboard/Telegram/eventi).</p>
</body></html>"""

    text = (f"MATRICE SCARICO PREVENTIVO — RUSPINO ({now_iso})\n"
            + ('*** SCARICO PREVENTIVO consigliato (Critico raggiunto) ***\n' if res['sp3'] else '')
            + '\n'.join(text_rows)
            + f"\n\nEnsemble {res['n_models']}/5 modelli, {res['n_points_ok']}/11 punti · {metno_note}\n"
            "Soglie = dashboard Ruspino v2.0, matrice 3×2 (Att 60/80/100 · Crit 80/120/140).\n"
            "Contenuto riservato ai destinatari email.")
    return subject, text, html


def compose_email_days(res, now_iso, label='Sorgenti Panna'):
    lvl_name = PANNA_LEVELS[res['max_level']] if res['max_level'] >= 0 else 'sotto soglia'
    icons = {0: '⚠️', 1: '⛔', 2: '⚡'}
    subject = f"{icons.get(res['max_level'], '✓')} Forecast soglie {label} — {lvl_name} (worst-case giornaliero)"

    rows_html, rows_text = [], []
    for i, d in enumerate(res['days']):
        color = PANNA_COLORS[d['level_idx']] if d['level_idx'] >= 0 else '#94a3b8'
        lvn = PANNA_LEVELS[d['level_idx']] if d['level_idx'] >= 0 else 'sotto soglia'
        metno_s = f"{d['metno']:.1f}" if d['metno'] is not None else 'N/D'
        part = f" · copertura {d['n_hours']}/24h" if d['n_hours'] < 24 else ''
        rows_html.append(
            f'<tr><td style="padding:8px 12px;border:1px solid #e2e8f0;font-weight:600">Giorno {i+1}<br>'
            f'<span style="font-size:11px;color:#94a3b8">{d["date"]}</span></td>'
            f'<td style="padding:8px 12px;border:1px solid #e2e8f0;text-align:center;font-size:15px;'
            f'font-weight:700;color:{color}">{d["worst"]:.1f} mm/g</td>'
            f'<td style="padding:8px 12px;border:1px solid #e2e8f0;color:{color};font-weight:600">{lvn}</td>'
            f'<td style="padding:8px 12px;border:1px solid #e2e8f0;font-size:12px;color:#334155">'
            f'{d["worst_model"]} · media ens. {d["mean"]:.1f} · MET.no {metno_s} · '
            f'concordanza {d["n_agree"]}/{d["n_models"]}{part}</td></tr>')
        rows_text.append(f"  Giorno {i+1} ({d['date']}): worst {d['worst']:.1f} mm/g ({d['worst_model']}) "
                         f"→ {lvn} · media {d['mean']:.1f} · MET.no {metno_s} · "
                         f"concordanza {d['n_agree']}/{d['n_models']}{part}")

    metno_note = (f"MET Norway: copertura oraria {res['metno_cov_h']}/{FORECAST_HOURS}h "
                  "(validazione indipendente, mai mediato)")
    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#0f172a">
<h2 style="margin:0 0 4px">🌧️ Forecast soglie — {label}</h2>
<p style="color:#64748b;font-size:12px;margin:0 0 14px">Cumulata giornaliera worst-case (criterio B, dashboard v6.6):
Attenzione ≥{PANNA_DAY_THR['att']} · Critico ≥{PANNA_DAY_THR['crit']} · Estremo ≥{PANNA_DAY_THR['ext']} mm/giorno · run {now_iso}</p>
<table style="border-collapse:collapse">
<tr><th style="padding:8px 12px;border:1px solid #e2e8f0"></th>
<th style="padding:8px 12px;border:1px solid #e2e8f0;font-size:12px;color:#64748b">Worst-case</th>
<th style="padding:8px 12px;border:1px solid #e2e8f0;font-size:12px;color:#64748b">Livello</th>
<th style="padding:8px 12px;border:1px solid #e2e8f0;font-size:12px;color:#64748b">Dettaglio</th></tr>
{''.join(rows_html)}
</table>
<p style="font-size:11px;color:#94a3b8;margin:14px 0 0">
Ensemble: {res['n_models']}/5 modelli Open-Meteo, 11 punti pesati ({res['n_points_ok']}/11 OK) · {metno_note}.<br>
Giorni civili Europe/Rome. Contenuto riservato ai destinatari email.</p>
</body></html>"""
    text = (f"FORECAST SOGLIE — SORGENTI PANNA ({now_iso})\n"
            + '\n'.join(rows_text)
            + f"\n\nSoglie = dashboard Panna v6.6 (5/10/15 mm/g worst-case, giorni civili Europe/Rome).\n"
            f"Ensemble {res['n_models']}/5 modelli, {res['n_points_ok']}/11 punti · {metno_note}\n"
            "Contenuto riservato ai destinatari email.")
    return subject, text, html


# ─── Stato anti-spam (minimo indispensabile: il repo è pubblico) ────────────

def load_state(f):
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _worsts_by_horizon(res):
    """Estrae i worst-case per orizzonte da un risultato (matrice Ruspino o
    giornaliero Panna/Cepina). Ritorna dict {chiave_orizzonte: mm}."""
    if 'rows' in res:      # Ruspino: cumulate mobili 24/48/72h
        return {r['hz']['sub']: r['worst'] for r in res['rows']}
    if 'days' in res:      # Panna/Cepina: worst-case giornaliero (giorni 1..3)
        return {f'g{i+1}': d['worst'] for i, d in enumerate(res['days'])}
    return {}


# Soglie del "salto brusco" (Canale B): una variazione del worst-case rispetto
# al run precedente merita una mail anche SOTTO la soglia critica, se il peggio-
# ramento è consistente. Servono ENTRAMBE le condizioni per evitare rumore su
# valori piccoli: +JUMP_ABS_MM assoluti E +JUMP_REL_PCT relativi, su almeno un
# orizzonte. Valori scelti pragmaticamente (dichiarati, tarabili via env).
JUMP_ABS_MM = float(os.environ.get('FC_JUMP_ABS_MM', '20'))   # +20 mm
JUMP_REL_PCT = float(os.environ.get('FC_JUMP_REL_PCT', '50')) # +50%
JUMP_MIN_MM = float(os.environ.get('FC_JUMP_MIN_MM', '25'))   # ignora salti sotto 25mm assoluti
JUMP_RENOTIFY_H = float(os.environ.get('FC_JUMP_RENOTIFY_H', '6'))  # non ripetere jump < 6h


def _hours_since(iso, now_dt):
    try:
        return (now_dt - datetime.fromisoformat(iso.replace('Z', '+00:00'))).total_seconds() / 3600
    except Exception:
        return 1e9


def decide_send(state, area, rank, worsts, today, now_dt):
    """Decide SE e PERCHÉ inviare. Ritorna (send: bool, reason: str, kind: str).
    Due canali:
      A) SOGLIA: rank>=1 (livello Att/Crit raggiunto). 1 mail/giorno + escalation.
      B) SALTO: un orizzonte peggiora di >=JUMP_ABS_MM e >=JUMP_REL_PCT rispetto
         al run precedente (anche sotto soglia critica), con guardia anti-ripetizione.
    La guardia condivisa evita mail-doppione: stesso livello + nessun salto +
    già avvisato oggi → silenzio.
    """
    st = state.get(area, {})
    prev_worsts = st.get('worsts', {})

    # ── Canale B: salto brusco ──
    jump_hz, jump_from, jump_to = None, None, None
    for hz, cur in worsts.items():
        prev = prev_worsts.get(hz)
        if prev is None:
            continue
        delta = cur - prev
        rel = (delta / prev * 100) if prev > 0.5 else (999 if delta >= JUMP_MIN_MM else 0)
        if cur >= JUMP_MIN_MM and delta >= JUMP_ABS_MM and rel >= JUMP_REL_PCT:
            if jump_to is None or cur > jump_to:
                jump_hz, jump_from, jump_to = hz, prev, cur
    if jump_hz is not None:
        # Guardia: non ripetere lo stesso salto entro JUMP_RENOTIFY_H se il
        # valore non è ulteriormente salito.
        last_jump = st.get('last_jump', {})
        if (last_jump.get('hz') == jump_hz
                and _hours_since(last_jump.get('utc', '1970-01-01T00:00:00Z'), now_dt) < JUMP_RENOTIFY_H
                and jump_to <= last_jump.get('to', 0) + 5):
            pass  # già avvisato di questo salto di recente → non ripetere
        else:
            # Se il salto coincide anche col superamento soglia (rank>=1),
            # l'informazione è doppia: lo dichiaro nel motivo.
            soglia_txt = f" + soglia livello {rank}" if rank >= 1 else ''
            return True, f"salto {jump_hz}: {jump_from:.0f}→{jump_to:.0f} mm (+{jump_to-jump_from:.0f}){soglia_txt}", 'jump'

    # ── Canale A: soglia ──
    if rank <= 0:
        return False, 'sotto soglia', 'none'
    if st.get('date') != today:
        return True, f"soglia raggiunta (livello {rank})", 'threshold'
    if rank > st.get('rank', 0):
        return True, f"escalation a livello {rank}", 'threshold'

    return False, 'già avvisato oggi, nessuna escalation/salto', 'none'


def compose_telegram_matrix(res, label, now_iso):
    """Versione compatta per Telegram della matrice/soglie previsionali.
    Gestisce sia il formato 'days' (Panna/Cepina, cumulata giornaliera) sia
    'rows' (Ruspino, matrice 24/48/72h). Va agli stessi destinatari dell'email
    (telegram_chat_ids in areas.json, o chat di default). Markdown semplice;
    se dovesse fallire il parse, il sender robusto ripiega in testo semplice."""
    if 'days' in res:
        levels = PANNA_LEVELS
        lvl_name = levels[res['max_level']] if res['max_level'] >= 0 else 'sotto soglia'
        lines = [f"\U0001F327 *Forecast soglie \u2014 {label}*",
                 f"Livello: *{lvl_name.upper()}* (worst-case giornaliero)", ""]
        for i, d in enumerate(res['days']):
            lvn = levels[d['level_idx']] if d['level_idx'] >= 0 else 'sotto soglia'
            val = f"*{d['worst']:.1f} mm/g*" if d['level_idx'] >= 0 else f"{d['worst']:.1f} mm/g"
            lines.append(f"G{i+1} {d['date'][5:]}: {val} \u2014 {lvn}")
    else:
        levels = MATRIX2['levels']
        lvl_name = levels[res['max_level']] if res['max_level'] >= 0 else 'sotto soglia'
        head = ("\u26D4 *SCARICO PREVENTIVO consigliato*" if res.get('sp3')
                else f"Livello: *{lvl_name.upper()}*")
        lines = [f"\U0001F6B1 *Matrice scarico \u2014 {label}*", head, ""]
        for r in res['rows']:
            lvn = levels[r['level_idx']] if r['level_idx'] >= 0 else 'sotto soglia'
            val = f"*{r['worst']:.0f} mm*" if r['level_idx'] >= 0 else f"{r['worst']:.0f} mm"
            lines.append(f"{r['hz']['label']}: {val} \u2014 {lvn}")
    lines.append("")
    lines.append(f"_Ensemble {res['n_models']}/5 modelli \u00b7 "
                 f"{res['n_points_ok']}/11 punti \u00b7 MET Norway indip._")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true', help='Calcola ma non invia')
    ap.add_argument('--force', action='store_true', help='Invia anche se sotto soglia / già inviata oggi')
    args = ap.parse_args()

    archive_dir = Path(__file__).resolve().parents[1]
    state_file = archive_dir / 'state' / 'forecast_matrix_state.json'
    state = load_state(state_file)
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    today = datetime.now(tz=timezone.utc).date().isoformat()

    jobs = [
        ('ruspino', 'Ruspino', compute_ruspino_matrix, compose_email_ruspino),
        ('panna',   'Sorgenti Panna',
         lambda: compute_days_worstcase(CONTROL_POINTS),
         lambda res, ts: compose_email_days(res, ts, 'Sorgenti Panna')),
        ('cepina',  'Cepina',
         lambda: compute_days_worstcase(POINTS_CEPINA),
         lambda res, ts: compose_email_days(res, ts, 'Cepina')),
    ]

    now_dt = datetime.now(tz=timezone.utc)
    for area, label, compute, compose in jobs:
        log.info(f'[{label}] calcolo matrice previsionale 24/48/72h...')
        try:
            res = compute()
        except Exception as e:
            log.error(f'[{label}] calcolo fallito: {e}', exc_info=True)
            continue
        if not res:
            log.warning(f'[{label}] nessun dato ensemble disponibile')
            continue

        rank = res['max_level'] + 1        # -1→0, 0→1, ...
        lvl = ('sotto soglia' if res['max_level'] < 0 else
               (MATRIX2['levels'] if area == 'ruspino' else PANNA_LEVELS)[res['max_level']])
        worsts = _worsts_by_horizon(res)
        log.info(f'[{label}] livello {lvl} (rank {rank}) · worst: '
                 + ' '.join(f'{k}={v:.0f}' for k, v in worsts.items()))

        send, reason, kind = decide_send(state, area, rank, worsts, today, now_dt)
        if args.force:
            send, reason, kind = True, 'forzato', kind if kind != 'none' else 'threshold'
        if not send:
            log.info(f'[{label}] nessun invio ({reason})')
            # Aggiorno comunque i worst salvati per il confronto del prossimo run
            prev = state.get(area, {})
            prev['worsts'] = worsts
            state[area] = prev
            continue

        subject, text, html = compose(res, now_iso)
        # Se è un trigger da SALTO (Canale B), lo dichiaro in testa alla mail:
        # l'utente deve capire che è un cambio repentino, non il run periodico.
        if kind == 'jump':
            jump_banner = f"⚡ CAMBIO REPENTINO PREVISIONI — {reason}\n\n"
            subject = f"⚡ [CAMBIO] {subject}"
            text = jump_banner + text
            if html:
                html = (f'<div style="background:#78350f;color:#fde68a;padding:10px 14px;'
                        f'border-radius:8px;margin:0 0 12px;font-weight:600">⚡ Cambio repentino '
                        f'previsioni — {reason}</div>') + html
        log.info(f'[{label}] INVIO ({kind}): {reason}')
        if args.dry_run:
            log.info(f'[{label}] DRY-RUN — subject: {subject}\n{text[:200]}')
            continue

        rcpt_email, rcpt_tg = _area_recipients(area)
        status = send_email(subject, text, html, to=rcpt_email)
        log.info(f'[{label}] email matrice: {status}')
        # Stesso contenuto, compatto, anche su Telegram. Stesso gate
        # anti-spam dell'email (decide_send): nessuna raffica in piu'.
        tg_md = compose_telegram_matrix(res, label, now_iso)
        if kind == 'jump':
            tg_md = f'\u26a1 *CAMBIO REPENTINO PREVISIONI* \u2014 {reason}\n\n' + tg_md
        tg_status = send_telegram(tg_md, chat_ids=rcpt_tg)
        log.info(f'[{label}] telegram matrice: {tg_status}')
        # Notifica considerata inviata se ha funzionato almeno un canale.
        if status == 'true' or tg_status == 'true':
            entry = {'date': today, 'rank': max(rank, 0), 'last_sent_utc': now_iso,
                     'worsts': worsts}
            # Preserva il rank del giorno: un invio da salto sotto soglia non
            # deve azzerare l'escalation già raggiunta oggi.
            prev = state.get(area, {})
            if prev.get('date') == today:
                entry['rank'] = max(rank, prev.get('rank', 0))
            if kind == 'jump':
                # Ricava l'orizzonte/valore del salto per la guardia anti-ripetizione
                hz = reason.split()[1].rstrip(':') if len(reason.split()) > 1 else '?'
                entry['last_jump'] = {'hz': hz, 'to': max(worsts.values()) if worsts else 0,
                                      'utc': now_iso}
            elif prev.get('date') == today and prev.get('last_jump'):
                entry['last_jump'] = prev['last_jump']   # conserva stato salto del giorno
            state[area] = entry

    state['_last_run_utc'] = now_iso
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
