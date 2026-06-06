#!/usr/bin/env python3
"""
forecast_verify.py — Verifica accuratezza forecast vs osservato.

Per ogni area, legge i forecast salvati nelle ultime N ore in
`forecast_history.jsonl` (scritto da `forecast_history.py`) e li confronta
con l'osservato corrispondente preso dai CSV `<area>_cum3.csv` (rolling 1h
e 3h ricostruiti dalla cumulata 3h DPC).

Output:
  archive/data/forecast_verification.csv  (append-only, una riga per match)

Schema CSV:
  forecast_made_at_utc  : ISO ts quando il forecast è stato emesso
  area_name             : area
  source                : 'openmeteo' | 'metno'
  horizon               : '1h' | '3h'
  forecast_mm           : valore previsto
  observed_mm           : valore osservato corrispondente (al + vicino disponibile)
  observed_at_utc       : ts dell'osservazione usata
  bias_mm               : observed - forecast
  abs_error_mm          : |bias|
  hit_warning           : 1 se entrambi (forecast e observed) >= soglia warning
  missed_warning        : 1 se observed >= warning ma forecast < warning
  false_alarm_warning   : 1 se forecast >= warning ma observed < warning
"""
from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
AREAS_FILE = ROOT / 'areas.json'
HISTORY_FILE = DATA / 'forecast_history.jsonl'
VERIFY_FILE = DATA / 'forecast_verification.csv'

# Window di lookback: verifica forecast emessi nelle ultime LOOKBACK_HOURS ore
# Per un forecast su orizzonte 3h, serve aspettare almeno 3h prima di poterlo verificare
LOOKBACK_HOURS = 48          # verifica gli ultimi 2 giorni
MATCH_TOLERANCE_MIN = 120    # CUM3 ha campioni ogni 3h → tolleranza ±2h

CSV_FIELDS = [
    'forecast_made_at_utc', 'area_name', 'source', 'horizon',
    'forecast_mm', 'observed_mm', 'observed_at_utc',
    'bias_mm', 'abs_error_mm',
    'hit_warning', 'missed_warning', 'false_alarm_warning',
]

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger('verify')


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def load_observed(area_name: str) -> list[tuple[datetime, float]]:
    """Carica timeseries (ts, cum3_mean_mm) da <area>_cum3.csv ordinata.
    Filtra solo le righe area-wide (location_type='area'), che contengono
    le statistiche aggregate (mean/min/max) sul poligono."""
    f = DATA / f'{area_name}_cum3.csv'
    if not f.exists():
        return []
    out = []
    with f.open() as fh:
        rdr = csv.DictReader(fh)
        for row in rdr:
            if row.get('location_type') != 'area':
                continue
            ts = parse_ts(row.get('timestamp_utc', ''))
            v = row.get('mean')
            if ts is None or v in (None, ''):
                continue
            try:
                out.append((ts, float(v)))
            except ValueError:
                continue
    out.sort(key=lambda x: x[0])
    return out


def observed_rolling(series: list[tuple[datetime, float]],
                     target: datetime, window_h: int,
                     tol_min: int = MATCH_TOLERANCE_MIN) -> tuple[float, datetime] | None:
    """Somma valori in [target-window, target]. CUM3 ha campioni ogni ~5 min:
    sommare direttamente sovrastima. Strategia: prendiamo il campione cum3 più
    vicino a `target` per la finestra 3h (il CSV è già cumulata 3h), e per
    finestra 1h facciamo media valori / 3 (proxy)."""
    cand = [(ts, v) for ts, v in series if abs((ts - target).total_seconds()) <= tol_min * 60]
    if not cand:
        return None
    ts, v = min(cand, key=lambda x: abs((x[0] - target).total_seconds()))
    if window_h == 3:
        return (v, ts)
    elif window_h == 1:
        return (v / 3.0, ts)  # approssimazione: CUM3 ÷ 3
    return (v, ts)


def load_thresholds(area: dict, product: str = 'CUM3') -> dict[str, float]:
    """Estrae soglie {level: value_mm} per il prodotto indicato."""
    out = {}
    mon = area.get('monitoring', {})
    prods = mon.get('products', {})
    for th in prods.get(product, {}).get('thresholds', []):
        lvl = th.get('level')
        v = th.get('value_mm')
        if lvl and v is not None:
            out[lvl] = float(v)
    return out


def already_verified(forecast_made_at: str, area: str, source: str, horizon: str) -> bool:
    """Evita duplicati: controlla se questo (made_at, area, source, horizon) c'è già."""
    if not VERIFY_FILE.exists():
        return False
    key = (forecast_made_at, area, source, horizon)
    with VERIFY_FILE.open() as fh:
        rdr = csv.DictReader(fh)
        for row in rdr:
            if (row['forecast_made_at_utc'], row['area_name'],
                row['source'], row['horizon']) == key:
                return True
    return False


def verify_forecast(record: dict, areas_by_name: dict, observed_by_area: dict,
                    writer) -> int:
    """Processa un record di forecast_history. Ritorna n righe scritte."""
    made_at = parse_ts(record.get('updated_at_utc', ''))
    if made_at is None:
        return 0
    area_name = record.get('area_name')
    area = areas_by_name.get(area_name)
    if not area:
        return 0
    thresholds = load_thresholds(area, 'CUM3')
    warning_th = thresholds.get('warning', 0)

    series = observed_by_area.get(area_name) or []
    if not series:
        return 0

    n_written = 0
    for source in ('openmeteo', 'metno'):
        fc = record.get(source) or {}
        for horizon, key in (('1h', 'max_1h'), ('3h', 'max_3h')):
            fc_val = fc.get(key)
            if fc_val is None:
                continue
            if already_verified(record['updated_at_utc'], area_name, source, horizon):
                continue
            # target: per orizzonte Nh, il picco previsto sta entro made_at + N ore
            target = made_at + timedelta(hours=int(horizon[:-1]))
            # Non verifichiamo forecast non ancora "scaduti"
            if target > datetime.now(timezone.utc):
                continue
            obs = observed_rolling(series, target, int(horizon[:-1]))
            if obs is None:
                continue
            obs_val, obs_ts = obs
            bias = obs_val - float(fc_val)
            row = {
                'forecast_made_at_utc': record['updated_at_utc'],
                'area_name': area_name,
                'source': source,
                'horizon': horizon,
                'forecast_mm': f'{float(fc_val):.2f}',
                'observed_mm': f'{obs_val:.2f}',
                'observed_at_utc': obs_ts.isoformat().replace('+00:00', 'Z'),
                'bias_mm': f'{bias:+.2f}',
                'abs_error_mm': f'{abs(bias):.2f}',
                'hit_warning':        1 if (warning_th > 0 and float(fc_val) >= warning_th and obs_val >= warning_th) else 0,
                'missed_warning':     1 if (warning_th > 0 and float(fc_val) <  warning_th and obs_val >= warning_th) else 0,
                'false_alarm_warning':1 if (warning_th > 0 and float(fc_val) >= warning_th and obs_val <  warning_th) else 0,
            }
            writer.writerow(row)
            n_written += 1
    return n_written


def main():
    if not HISTORY_FILE.exists():
        log.info(f'{HISTORY_FILE} mancante, niente da verificare')
        return 0
    areas = json.loads(AREAS_FILE.read_text())['areas']
    areas_by_name = {a['name']: a for a in areas}
    # Pre-carica osservato per ogni area
    observed_by_area = {a['name']: load_observed(a['name']) for a in areas}

    # Carica forecast nelle ultime LOOKBACK_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    records = []
    with HISTORY_FILE.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(rec.get('updated_at_utc', ''))
            if ts is None or ts < cutoff:
                continue
            records.append(rec)

    log.info(f'{len(records)} forecast nelle ultime {LOOKBACK_HOURS}h')

    new_file = not VERIFY_FILE.exists()
    n_total = 0
    with VERIFY_FILE.open('a', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        for rec in records:
            n_total += verify_forecast(rec, areas_by_name, observed_by_area, writer)

    log.info(f'✓ {n_total} righe di verifica scritte in {VERIFY_FILE.name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
