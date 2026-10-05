#!/usr/bin/env python3
"""
sri_collect.py — archivio del radar DPC "puro" (SRI, mm/h ogni 5') per area.

PERCHE'
  La CUM3 DPC NON è radar: è ottenuta solo dai pluviometri a terra interpolati.
  Per stimare la pioggia di un evento DA RADAR servono le intensità SRI (radar
  nazionale, 5'), integrate nel tempo. ARPA copre solo la Lombardia: per Panna
  l'SRI è l'unica stima radar. Il nowcast scarica l'SRI ma non lo archivia.

COSA FA
  Ogni run scarica i frame SRI mancanti (passo 5') dall'ultimo archiviato fino
  all'ultimo disponibile, max MAX_FRAMES per run (oldest-first, recupera i buchi),
  e per ogni area di areas.json salva max/mean d'area (mm/h). Un GeoTIFF serve
  tutte le aree. Frame non scaricabile e più vecchio di 60': riga con
  status=missing (mai 0: un buco non è "asciutto").

OUTPUT
  archive/data/<area>_sri.csv           raccolta continua (ogni 10')
  archive/data/<area>_sri_backfill.csv  frame storici recuperati (--backfill-episodes)
    timestamp_utc,area_name,max_mmh,mean_mmh,pixel_count,status,fetched_at_utc

USO
  python archive/scripts/sri_collect.py                    # raccolta incrementale
  python archive/scripts/sri_collect.py --probe            # quanto indietro tiene l'API DPC?
  python archive/scripts/sri_collect.py --backfill-episodes  # frame delle finestre episodio
                                                           # (solo se l'API li ha ancora)
Env: SRI_MAX_FRAMES (default 36 = 3h per run), SRI_LOOKBACK_H (default 6).
"""
import argparse
import csv
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DPC_API = 'https://radar-api.protezionecivile.it'
BASE = Path(__file__).resolve().parent.parent          # archive/
DATA = BASE / 'data'
AREAS_FILE = BASE / 'areas.json'
PRODUCT = 'SRI'
STEP = timedelta(minutes=5)
MAX_FRAMES = int(os.environ.get('SRI_MAX_FRAMES', '36'))
LOOKBACK_H = float(os.environ.get('SRI_LOOKBACK_H', '6'))
MISSING_AFTER = timedelta(minutes=60)
TM_PROJ = '+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs'
FIELDS = ['timestamp_utc', 'area_name', 'max_mmh', 'mean_mmh', 'pixel_count', 'status', 'fetched_at_utc']
UTC = timezone.utc

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('sri')
_session = requests.Session()
_session.headers['User-Agent'] = 'radar-dpc-sri-collect/1.0'


def iso(d):
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(s):
    return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)


def floor5(d):
    return d.replace(minute=d.minute - d.minute % 5, second=0, microsecond=0)


# ── API DPC ──────────────────────────────────────────────────────────────────
def latest_ts():
    r = _session.get(f'{DPC_API}/findLastProductByType', params={'type': PRODUCT}, timeout=60)
    r.raise_for_status()
    items = r.json().get('lastProducts', [])
    if not items:
        raise RuntimeError('nessun prodotto SRI')
    return datetime.fromtimestamp(items[0]['time'] / 1000, tz=UTC)


def presigned_url(ts):
    """URL S3 del frame, o (None, http_status)."""
    try:
        r = _session.post(f'{DPC_API}/downloadProduct', timeout=60,
                          json={'productType': PRODUCT, 'productDate': int(ts.timestamp() * 1000)})
    except requests.RequestException as e:
        return None, f'err {e.__class__.__name__}'
    if not r.ok:
        return None, r.status_code
    try:
        return r.json().get('url'), r.status_code
    except ValueError:
        return None, 'json'


def download(ts):
    url, st = presigned_url(ts)
    if not url:
        return None, st
    for attempt in range(3):
        try:
            rr = _session.get(url, timeout=60)
            if rr.ok and len(rr.content) > 256:
                return rr.content, 200
            st = rr.status_code
        except requests.RequestException as e:
            st = f'err {e.__class__.__name__}'
        time.sleep(2 * (attempt + 1))
    return None, st


# ── Statistiche d'area (poligono riproiettato nel CRS del raster) ────────────
def area_stats(tiff, polygon_latlon):
    import numpy as np
    import rasterio
    import rasterio.mask
    from rasterio.warp import transform_geom
    from shapely.geometry import Polygon, mapping
    geom = mapping(Polygon([(lon, lat) for lat, lon in polygon_latlon]))
    with rasterio.open(io.BytesIO(tiff)) as src:
        dst_crs = src.crs if src.crs else TM_PROJ
        g = transform_geom('EPSG:4326', dst_crs, geom)
        try:
            arr, _ = rasterio.mask.mask(src, [g], crop=True, all_touched=True,
                                        nodata=src.nodata if src.nodata is not None else -9999)
        except ValueError:
            return None
        nodata = src.nodata if src.nodata is not None else -9999
    a = arr[0].astype('float64')
    ok = (a != nodata) & np.isfinite(a) & (a > -900) & (a < 1000)
    if not ok.any():
        return None
    v = np.clip(a[ok], 0, None)
    return {'max': float(v.max()), 'mean': float(v.mean()), 'n': int(v.size)}


# ── CSV ──────────────────────────────────────────────────────────────────────
# Il backfill scrive in un file separato: gira in parallelo alla raccolta
# ogni 10' senza conflitti di push sullo stesso CSV. episodes.py legge entrambi.
BACKFILL = False


def csv_path(area, backfill=None):
    bf = BACKFILL if backfill is None else backfill
    return DATA / (f'{area}_sri_backfill.csv' if bf else f'{area}_sri.csv')


def existing(area):
    out = set()
    for p in (csv_path(area, False), csv_path(area, True)):
        if p.exists():
            with open(p, newline='', encoding='utf-8') as f:
                out |= {r['timestamp_utc'] for r in csv.DictReader(f)}
    return out


def append(area, rows):
    p = csv_path(area)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def load_areas():
    return [a for a in json.loads(AREAS_FILE.read_text(encoding='utf-8'))['areas'] if a.get('polygon')]


# ── Raccolta ─────────────────────────────────────────────────────────────────
def collect(timestamps, areas, now, write_missing=True, newest_first=False):
    have = {a['name']: existing(a['name']) for a in areas}
    todo = sorted((t for t in set(timestamps) if any(iso(t) not in have[a['name']] for a in areas)),
                  reverse=newest_first)
    if len(todo) > MAX_FRAMES:
        log.info(f'{len(todo)} frame da scaricare, limite {MAX_FRAMES}: il resto ai prossimi run')
        todo = todo[:MAX_FRAMES]
    n_ok = n_miss = 0
    for ts in todo:
        tiff, st = download(ts)
        fetched = iso(now)
        out = {}
        if tiff is None:
            if now - ts < MISSING_AFTER or not write_missing:
                log.info(f'  {iso(ts)}: non ancora disponibile ({st}), riprovo dopo')
                continue
            log.warning(f'  {iso(ts)}: mancante ({st})')
            n_miss += 1
            for a in areas:
                out[a['name']] = {'timestamp_utc': iso(ts), 'area_name': a['name'], 'max_mmh': '',
                                  'mean_mmh': '', 'pixel_count': '', 'status': f'missing:{st}',
                                  'fetched_at_utc': fetched}
        else:
            n_ok += 1
            for a in areas:
                try:
                    s = area_stats(tiff, a['polygon'])
                except Exception as e:
                    log.warning(f'  {iso(ts)} {a["name"]}: stats fallite {e}')
                    s = None
                out[a['name']] = {'timestamp_utc': iso(ts), 'area_name': a['name'],
                                  'max_mmh': f"{s['max']:.2f}" if s else '',
                                  'mean_mmh': f"{s['mean']:.3f}" if s else '',
                                  'pixel_count': s['n'] if s else 0,
                                  'status': 'ok' if s else 'no_pixels', 'fetched_at_utc': fetched}
        for a in areas:
            if iso(ts) not in have[a['name']]:
                append(a['name'], [out[a['name']]])
                have[a['name']].add(iso(ts))
    log.info(f'SRI: {n_ok} frame archiviati, {n_miss} mancanti')
    return n_ok


def run_incremental():
    areas = load_areas()
    now = datetime.now(tz=UTC)
    last = latest_ts()
    # finestra: ultime LOOKBACK_H ore (i buchi recenti si riprovano finché non
    # diventano 'missing'); dopo un fermo più lungo si riparte dall'ultimo
    # frame archiviato e si recupera MAX_FRAMES per run, dal più vecchio.
    start = last - timedelta(hours=LOOKBACK_H)
    lasts = []
    for a in areas:                       # solo il file della raccolta continua
        p = csv_path(a['name'], False)
        if p.exists():
            with open(p, newline='', encoding='utf-8') as f:
                ts_ = [r['timestamp_utc'] for r in csv.DictReader(f)]
            if ts_:
                lasts.append(max(ts_))
    if len(lasts) == len(areas):
        start = min(start, parse_iso(min(lasts)) + STEP)
    ts, t = [], floor5(start)
    while t <= last:
        ts.append(t)
        t += STEP
    log.info(f'SRI ultimo disponibile {iso(last)}; finestra {iso(ts[0])} → {iso(ts[-1])}')
    collect(ts, areas, now)


def run_backfill_episodes():
    areas = load_areas()
    now = datetime.now(tz=UTC)
    ts = set()
    p = DATA / 'episodes.csv'
    with open(p, newline='', encoding='utf-8') as f:
        for e in csv.DictReader(f):
            s = floor5(parse_iso(e['start_utc'])) - timedelta(hours=1)
            en = parse_iso(e['end_utc']) + timedelta(hours=1)
            while s <= en:
                ts.add(s)
                s += STEP
    log.info(f'backfill episodi: {len(ts)} frame nelle finestre episodio')
    global BACKFILL
    BACKFILL = True
    # nel backfill un frame non scaricabile NON viene marcato mancante:
    # un errore transitorio non deve chiudere per sempre il buco
    collect(ts, areas, now, write_missing=False, newest_first=True)


def run_probe():
    """Verifica fino a quando l'API DPC restituisce frame SRI storici."""
    last = latest_ts()
    print(f'Ultimo SRI disponibile: {iso(last)}')
    print(f'{"indietro":>10}  {"timestamp":20}  {"downloadProduct":>15}  {"GeoTIFF":>8}')
    for label, d in (('10 min', timedelta(minutes=10)), ('1 h', timedelta(hours=1)),
                     ('6 h', timedelta(hours=6)), ('1 g', timedelta(days=1)),
                     ('2 g', timedelta(days=2)), ('3 g', timedelta(days=3)),
                     ('5 g', timedelta(days=5)), ('7 g', timedelta(days=7)),
                     ('10 g', timedelta(days=10)), ('14 g', timedelta(days=14)),
                     ('21 g', timedelta(days=21)), ('30 g', timedelta(days=30)),
                     ('60 g', timedelta(days=60)), ('90 g', timedelta(days=90)),
                     ('120 g', timedelta(days=120))):
        t = floor5(last - d)
        url, st = presigned_url(t)
        size = ''
        if url:
            try:
                rr = _session.get(url, timeout=60)
                size = f'{len(rr.content) // 1024} kB' if rr.ok else f'HTTP {rr.status_code}'
            except requests.RequestException as e:
                size = e.__class__.__name__
        print(f'{label:>10}  {iso(t):20}  {str(st):>15}  {size:>8}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--probe', action='store_true', help='verifica la profondità storica dell\'API DPC')
    ap.add_argument('--backfill-episodes', action='store_true', help='scarica i frame delle finestre episodio')
    a = ap.parse_args()
    try:
        if a.probe:
            run_probe()
        elif a.backfill_episodes:
            run_backfill_episodes()
        else:
            run_incremental()
    except Exception as e:
        log.error(f'sri_collect: {e}')
        sys.exit(1)
