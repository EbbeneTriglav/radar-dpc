#!/usr/bin/env python3
"""
arpa_collect.py — Fetch dati radar ARPA Lombardia (mosaico Desio + Flero).

Sorgente: https://radarlive.arpalombardia.it/CMP
- File raster GeoTIFF compressi (.tif.gz)
- Riflettività massima sulla verticale (MAX), in dBZ
- Scansione ogni 5 minuti, ultime 24 ore disponibili
- Nome file: CMPyymmddhhMM.MAX.tif.gz (UTC)

Aree monitorate: ruspino (Bergamo) e cepina (Valtellina).
Panna NON è coperto dai radar Lombardia (Toscana → resta solo DPC).

Conversione dBZ → mm/h (Marshall-Palmer Z = 200·R^1.6):
  R [mm/h] = (10^(dBZ/10) / 200) ^ (1/1.6)

Output:
  archive/data/<area>_arpa.csv  (CSV append-only, una riga per timestamp×area)

Schema CSV:
  timestamp_utc, area_name, location_type, location_name,
  max_dbz, mean_dbz, max_mmh, mean_mmh, pixel_count, fetched_at_utc

USO: chiamato dal workflow `arpa-collect.yml` ogni 5 min.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Usa i parametri EPSG ufficiali (silenzia il warning sui GeoTIFF ARPA, che
# hanno chiavi CRS leggermente diverse dal registro EPSG ma sono pur sempre 4326).
os.environ.setdefault('GTIFF_SRS_SOURCE', 'EPSG')

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.warp import transform_geom
from rasterio.mask import mask as rio_mask
from shapely.geometry import Polygon, mapping

# Riusa http_request del modulo comune (creato nel Blocco 3)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from radar_common import http_request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
AREAS_FILE = ROOT / 'areas.json'

ARPA_BASE = 'https://radarlive.arpalombardia.it/CMP'
ARPA_AREAS = {'ruspino', 'cepina'}        # solo queste sono coperte
USER_AGENT = 'Mozilla/5.0 (radar-dpc-arpa/1.0)'

# Soglia minima di riflettività per considerare "pioggia". Sotto questo valore
# mm/h = 0. Serve a eliminare il rumore di fondo: la formula Marshall-Palmer
# applicata a 0 dBZ darebbe ~0.036 mm/h anche col sereno. 5 dBZ ≈ 0.07 mm/h,
# convenzione prudente per non scartare pioggia debole reale ma azzerare il rumore.
MIN_DBZ_RAIN = 5.0

# CSV: una colonna per area, location_type=area (mean) e vertex (per ogni vertice poligono)
CSV_FIELDS = [
    'timestamp_utc', 'area_name', 'location_type', 'location_name',
    'lat', 'lon',
    'max_dbz', 'mean_dbz', 'min_dbz',
    'max_mmh', 'mean_mmh',
    'pixel_count', 'fetched_at_utc',
]

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger('arpa')


def dbz_to_mmh(dbz: float) -> float:
    """Marshall-Palmer: Z=200·R^1.6 → R=(10^(dBZ/10)/200)^(1/1.6).
    Sotto MIN_DBZ_RAIN restituisce 0 (rumore di fondo / no rain)."""
    if dbz is None or not np.isfinite(dbz) or dbz < MIN_DBZ_RAIN:
        return 0.0
    z_linear = 10.0 ** (dbz / 10.0)
    return float((z_linear / 200.0) ** (1.0 / 1.6))


def dbz_arr_to_mmh(arr: np.ndarray) -> np.ndarray:
    """Vettoriale: array di dBZ → array di mm/h. Sotto MIN_DBZ_RAIN → 0."""
    out = np.zeros_like(arr, dtype=np.float32)
    valid = np.isfinite(arr) & (arr >= MIN_DBZ_RAIN)
    z_linear = np.power(10.0, arr[valid] / 10.0)
    out[valid] = np.power(z_linear / 200.0, 1.0 / 1.6)
    return out


def utc_floor_5min(dt: datetime) -> datetime:
    """Arrotonda a 5 min sotto, in UTC."""
    dt = dt.astimezone(timezone.utc)
    m = (dt.minute // 5) * 5
    return dt.replace(minute=m, second=0, microsecond=0)


def list_available_filenames() -> list[str]:
    """Recupera la lista dei file disponibili dall'index del bucket ARPA.
    Strategia: prova il GET sulla directory listing; se fallisce, genera
    i candidati delle ultime 2 ore basandosi sul pattern noto."""
    r = http_request('GET', ARPA_BASE + '/', user_agent=USER_AGENT, retries=2)
    files = []
    if r is not None and r.ok:
        # Parser regex: trova tutte le occorrenze CMP<10digits>.MAX.tif.gz
        files = re.findall(r'CMP\d{10}\.MAX\.tif\.gz', r.text)
        files = sorted(set(files))
        log.info(f'  listing: {len(files)} file trovati nell\'index')
    if not files:
        # Fallback: genera ultimi 24 candidati (2h × 12 file/h)
        now = utc_floor_5min(datetime.now(timezone.utc))
        log.info('  listing fallito, uso fallback candidati ultime 5h')
        for i in range(60):
            t = now - timedelta(minutes=5 * i)
            files.append(f'CMP{t.strftime("%y%m%d%H%M")}.MAX.tif.gz')
        files.reverse()  # cronologico
    return files


def parse_timestamp(filename: str) -> datetime | None:
    """Estrae timestamp UTC dal nome file CMPyymmddhhMM.MAX.tif.gz"""
    m = re.match(r'CMP(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\.MAX\.tif\.gz', filename)
    if not m:
        return None
    yy, mo, dd, hh, mm = map(int, m.groups())
    return datetime(2000 + yy, mo, dd, hh, mm, tzinfo=timezone.utc)


def fetch_tiff(filename: str) -> bytes | None:
    """Scarica e decomprime un file .tif.gz dell'ARPA."""
    r = http_request('GET', f'{ARPA_BASE}/{filename}', user_agent=USER_AGENT, retries=2)
    if r is None or not r.ok or len(r.content) < 256:
        return None
    try:
        return gzip.decompress(r.content)
    except Exception as e:
        log.warning(f'  decompress {filename} fallito: {e}')
        return None


def stats_for_polygon_dbz(tiff_bytes: bytes, polygon_latlon: list[list[float]]) -> dict | None:
    """Stats dBZ + mm/h su un poligono dato in lat/lon.
    Riproietta automaticamente il poligono al CRS del raster ARPA
    (probabilmente UTM 32N / EPSG:32632 — verificato a runtime)."""
    # Poligono in lat/lon → GeoJSON
    poly = Polygon([(lon, lat) for lat, lon in polygon_latlon])
    geom_4326 = mapping(poly)

    with MemoryFile(tiff_bytes) as mf:
        with mf.open() as src:
            # Riproietta il poligono nel CRS del raster
            try:
                if src.crs and src.crs.to_epsg() != 4326:
                    geom = transform_geom('EPSG:4326', src.crs, geom_4326)
                else:
                    geom = geom_4326
            except Exception as e:
                log.warning(f'  riproiezione fallita: {e}')
                return None
            try:
                masked, _ = rio_mask(src, [geom], crop=True, filled=True)
            except ValueError as e:
                log.warning(f'  mask fallito: {e}')
                return None
            nodata = src.nodata if src.nodata is not None else -9999

    arr = masked[0].astype(np.float32)
    valid = (arr != nodata) & np.isfinite(arr) & (arr > -100)  # dBZ realistici
    vals_dbz = arr[valid]
    if vals_dbz.size == 0:
        return None
    # Converti in mm/h
    vals_mmh = dbz_arr_to_mmh(vals_dbz)
    return {
        'max_dbz':  float(np.max(vals_dbz)),
        'mean_dbz': float(np.mean(vals_dbz)),
        'min_dbz':  float(np.min(vals_dbz)),
        'max_mmh':  float(np.max(vals_mmh)),
        'mean_mmh': float(np.mean(vals_mmh)),
        'pixel_count': int(vals_dbz.size),
    }


def load_existing_timestamps(csv_file: Path) -> set[str]:
    """Per skippare file già processati."""
    if not csv_file.exists():
        return set()
    seen = set()
    with csv_file.open() as fh:
        rdr = csv.DictReader(fh)
        for row in rdr:
            if row.get('location_type') == 'area':
                seen.add(row['timestamp_utc'])
    return seen


def process_area(area: dict, files: list[str], max_new: int = 48) -> int:
    """Processa fino a `max_new` nuovi timestamp per l'area.
    Ritorna il numero di nuovi record scritti (uno per timestamp×location)."""
    name = area['name']
    csv_file = DATA / f'{name}_arpa.csv'
    seen = load_existing_timestamps(csv_file)
    log.info(f'[{name}] già processati: {len(seen)} timestamp')

    polygon = area.get('polygon')
    if not polygon:
        log.warning(f'[{name}] polygon mancante, skip')
        return 0

    # Filtra solo i file non ancora processati, prendi gli ultimi max_new
    new_files = []
    for f in files:
        ts = parse_timestamp(f)
        if ts is None:
            continue
        ts_iso = ts.isoformat().replace('+00:00', 'Z')
        if ts_iso not in seen:
            new_files.append((f, ts, ts_iso))
    # Ordina per timestamp CRESCENTE e prendi i più VECCHI tra i non
    # processati: così i buchi lasciati dai gap dello scheduler GitHub vengono
    # recuperati (la sorgente tiene ~24h) invece di restare permanenti, come
    # accaduto il 20/07/2026 (02:25–04:45 UTC persi durante l'evento Ruspino).
    # Con max_new=48 (4h di frame) un run recupera il gap tipico osservato.
    new_files.sort(key=lambda x: x[1])
    new_files = new_files[:max_new]
    if not new_files:
        log.info(f'[{name}] niente di nuovo')
        return 0

    log.info(f'[{name}] {len(new_files)} nuovi file da processare')
    rows = []
    fetched_at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

    for fname, ts, ts_iso in new_files:
        tiff = fetch_tiff(fname)
        if tiff is None:
            log.warning(f'  {fname}: download/decompress fallito')
            continue
        # Stats area (poligono)
        stats = stats_for_polygon_dbz(tiff, polygon)
        if stats is None:
            log.warning(f'  {fname}: nessun pixel valido nel poligono')
            continue
        log.info(f'  {fname}: max={stats["max_dbz"]:.1f} dBZ ({stats["max_mmh"]:.2f} mm/h), '
                 f'mean={stats["mean_dbz"]:.1f} dBZ ({stats["mean_mmh"]:.3f} mm/h), '
                 f'{stats["pixel_count"]} px')
        rows.append({
            'timestamp_utc': ts_iso, 'area_name': name,
            'location_type': 'area', 'location_name': name,
            'lat': '', 'lon': '',
            'max_dbz':  f'{stats["max_dbz"]:.2f}',
            'mean_dbz': f'{stats["mean_dbz"]:.2f}',
            'min_dbz':  f'{stats["min_dbz"]:.2f}',
            'max_mmh':  f'{stats["max_mmh"]:.3f}',
            'mean_mmh': f'{stats["mean_mmh"]:.3f}',
            'pixel_count': stats['pixel_count'],
            'fetched_at_utc': fetched_at,
        })

    if not rows:
        return 0
    # Append CSV (crea header se necessario)
    new_file = not csv_file.exists()
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    with csv_file.open('a', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def main() -> int:
    if not AREAS_FILE.exists():
        log.error(f'{AREAS_FILE} mancante'); return 1
    areas_all = json.loads(AREAS_FILE.read_text())['areas']
    areas = [a for a in areas_all if a['name'] in ARPA_AREAS]
    if not areas:
        log.info('Nessuna area ARPA configurata'); return 0

    files = list_available_filenames()
    if not files:
        log.error('Nessun file disponibile / listing fallito'); return 1
    log.info(f'Range temporale file: {files[0]} … {files[-1]}')

    total = 0
    for area in areas:
        total += process_area(area, files)
    log.info(f'✓ ARPA: {total} righe scritte ({len(areas)} aree)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
