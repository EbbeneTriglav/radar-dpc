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
import math
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


# ─── Rendering frame PNG per la mappa (bypass 403 lato browser) ──────────────
# ARPA blocca le richieste automatiche dirette (403), quindi il browser NON può
# scaricare i .tif.gz. Soluzione server-side: questo collector (che gira su
# GitHub Actions e i cui fetch ARPA a volte passano) rende gli ultimi frame come
# PNG RGBA leggeri + un index.json con i bounds LETTI dal file reale (rasterio),
# committati nel repo. arpa.html li legge da raw@main via L.imageOverlay.
# Nessun numero inventato: la georeferenziazione viene dai bounds effettivi del
# GeoTIFF, la scala colori dBZ è quella condivisa (allineata a colormap.js).
FRAMES_DIR = DATA / 'radar_arpa'
FRAMES_KEEP = 12                         # ultimi 12 frame = 1h a passo 5'

# Scala dBZ IDENTICA a js/colormap.js (stessi stop e colori)
_DBZ_SCALE = [
    (-30, (0, 0, 0, 0)), (0, (100, 149, 237, 60)), (10, (0, 230, 230, 140)),
    (20, (0, 200, 0, 180)), (30, (255, 255, 0, 200)), (35, (255, 180, 0, 215)),
    (40, (255, 80, 0, 230)), (45, (255, 0, 0, 240)), (55, (150, 0, 150, 250)),
    (65, (255, 0, 255, 255)),
]


def _dbz_to_rgba(arr, nodata):
    """Mappa un array dBZ → (H,W,4) uint8 con la scala condivisa. Interpolazione
    lineare tra gli stop, come ColorMap.getColor. nodata e valori <-100 → trasparente."""
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    valid = np.isfinite(arr) & (arr > -100) & (arr != nodata)
    vals = arr.copy()
    xs = np.array([s[0] for s in _DBZ_SCALE], dtype=np.float32)
    for ch in range(4):
        ys = np.array([s[1][ch] for s in _DBZ_SCALE], dtype=np.float32)
        interp = np.interp(np.clip(vals, xs[0], xs[-1]), xs, ys)
        chan = rgba[:, :, ch]
        chan[valid] = interp[valid].astype(np.uint8)
    rgba[~valid] = 0  # trasparente dove non c'è dato
    return rgba


def save_radar_frames_png(files: list[str]) -> int:
    """Rende gli ultimi FRAMES_KEEP frame ARPA come PNG + index.json.
    Ritorna il numero di frame scritti. Silenzioso su Pillow assente (best-effort)."""
    try:
        from PIL import Image
    except ImportError:
        log.warning('Pillow non installato: skip rendering frame PNG per la mappa')
        return 0

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    # Prendi gli ultimi FRAMES_KEEP timestamp validi (più recenti)
    parsed = []
    for f in files:
        ts = parse_timestamp(f)
        if ts:
            parsed.append((f, ts))
    parsed.sort(key=lambda x: x[1])
    recent = parsed[-FRAMES_KEEP:]
    if not recent:
        return 0

    index = []
    written = 0
    for fname, ts in recent:
        ts_iso = ts.isoformat().replace('+00:00', 'Z')
        png_name = f"{ts.strftime('%y%m%d%H%M')}.png"
        png_path = FRAMES_DIR / png_name
        if png_path.exists():
            # già reso: rileggi bounds dall'index se presente, altrimenti rigenera
            pass
        tiff = fetch_tiff(fname)
        if tiff is None:
            continue
        try:
            with MemoryFile(tiff) as mf:
                with mf.open() as src:
                    arr = src.read(1).astype(np.float32)
                    nodata = src.nodata if src.nodata is not None else -9999
                    b = src.bounds  # nativo del file
                    crs = src.crs
                    # Diagnostica CRS (una volta per run): scrive il CRS REALE
                    # letto dal file + bounds nativi + riproiettati, così è
                    # ispezionabile dal repo senza indovinare la proiezione.
                    if not getattr(save_radar_frames_png, '_crs_logged', False):
                        try:
                            dbg = FRAMES_DIR / '_crs_debug.txt'
                            wkt = crs.to_wkt() if crs else 'None'
                            proj4s = crs.to_proj4() if crs else 'None'
                            dbg.write_text(
                                f"file: {fname}\n"
                                f"crs.to_epsg(): {crs.to_epsg() if crs else None}\n"
                                f"crs.is_geographic: {crs.is_geographic if crs else None}\n"
                                f"crs.to_proj4(): {proj4s}\n"
                                f"bounds_native: {b.left}, {b.bottom}, {b.right}, {b.top}\n"
                                f"size: {src.width}x{src.height}\n"
                                f"crs.to_wkt():\n{wkt}\n"
                            )
                            log.info(f'  CRS ARPA reale: epsg={crs.to_epsg() if crs else None} '
                                     f'proj4={proj4s[:80] if crs else None}')
                        except Exception as _e:
                            log.warning(f'  crs debug write failed: {_e}')
                        save_radar_frames_png._crs_logged = True
                    # I file ARPA NON sono in gradi: hanno una proiezione custom
                    # (codice EPSG 32767 "user-defined", coordinate in metri).
                    # rasterio conosce comunque il CRS reale (stesso usato dal
                    # collector per le stats). Riproiettiamo i bounds a 4326
                    # ogni volta che il CRS NON è già geografico, senza affidarci
                    # a to_epsg() (che è None per il 32767).
                    is_geographic = bool(crs and crs.is_geographic)
                    if crs and not is_geographic:
                        from rasterio.warp import transform_bounds
                        left, bottom, right, top = transform_bounds(crs, 'EPSG:4326',
                                                                    b.left, b.bottom, b.right, b.top)
                    else:
                        left, bottom, right, top = b.left, b.bottom, b.right, b.top
                    # Guardia: se dopo la riproiezione i bounds NON sono
                    # plausibili lat/lon per la Lombardia, non scrivere l'index
                    # con dati errati (meglio nessun frame che un frame mal
                    # georeferenziato). Nessun numero inventato.
                    if not (-180 <= left <= 180 and -180 <= right <= 180
                            and -90 <= bottom <= 90 and -90 <= top <= 90):
                        log.warning(f'  bounds non geografici dopo riproiezione '
                                    f'({left:.0f},{bottom:.0f},{right:.0f},{top:.0f}) '
                                    f'crs={crs} — frame {fname} scartato')
                        continue
            rgba = _dbz_to_rgba(arr, nodata)
            Image.fromarray(rgba, 'RGBA').save(png_path, optimize=True)
            index.append({
                'file': png_name, 'ts_utc': ts_iso,
                # Leaflet imageOverlay vuole [[south, west],[north, east]]
                'bounds': [[round(bottom, 6), round(left, 6)],
                           [round(top, 6), round(right, 6)]],
            })
            written += 1
        except Exception as e:
            log.warning(f'  rendering PNG {fname} fallito: {e}')

    # Prune: elimina PNG non più nell'index (più vecchi di FRAMES_KEEP)
    keep_names = {e['file'] for e in index}
    for p in FRAMES_DIR.glob('*.png'):
        if p.name not in keep_names:
            try:
                p.unlink()
            except OSError:
                pass

    if index:
        index.sort(key=lambda e: e['ts_utc'])
        (FRAMES_DIR / 'index.json').write_text(json.dumps({
            'updated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
            'frames': index,
        }, indent=1))
        log.info(f'✓ radar frames PNG: {written} scritti, index.json aggiornato ({len(index)} frame)')
    return written


# ─── ARCHIVIO EVENTI per replay ────────────────────────────────────────────
# Quando è attivo un evento "cella su area" (storm_on_area senza storm_cleared
# successivo in events.csv) per un'area coperta da ARPA, archiviamo i frame
# radar RITAGLIATI attorno all'area (+20 km di buffer) in una cartella evento
# permanente, con un suo index.json. Un events_index.json elenca tutti gli
# eventi archiviati per il selettore nella pagina. Nessun prune: gli eventi
# restano per il replay futuro (peso stimato ~2 MB/evento, ben sotto i limiti).
EVENTS_DIR = FRAMES_DIR / 'events'
EVENT_BUFFER_KM = 20.0
EVENTS_CSV = DATA / 'events.csv'


def active_storm_events():
    """Legge events.csv e ritorna gli eventi 'cella su area' ATTUALMENTE attivi
    per le aree ARPA. Un evento è attivo se l'ultima riga per (area) è
    storm_on_area (non ancora storm_cleared). Ritorna {area: event_start_iso}."""
    if not EVENTS_CSV.exists():
        return {}
    import csv as _csv
    last_state = {}   # area -> ('on'|'cleared', start_iso)
    try:
        with EVENTS_CSV.open(newline='') as f:
            for row in _csv.DictReader(f):
                area = row.get('area_name')
                lvl = row.get('level')
                ts = row.get('event_timestamp_utc', '')
                if area not in ARPA_AREAS:
                    continue
                if lvl == 'storm_on_area':
                    last_state[area] = ('on', ts)
                elif lvl == 'storm_cleared':
                    last_state[area] = ('cleared', ts)
    except Exception as e:
        log.warning(f'  lettura events.csv fallita: {e}')
        return {}
    return {a: st[1] for a, st in last_state.items() if st[0] == 'on'}


def _area_bbox_native(area, crs):
    """Bounding box dell'area (dal polygon in areas.json) espansa di
    EVENT_BUFFER_KM, nel CRS NATIVO del raster (metri). Ritorna
    (left, bottom, right, top) o None."""
    poly = area.get('polygon') or []
    if not poly:
        return None
    lats = [p[0] for p in poly]
    lons = [p[1] for p in poly]
    south, north = min(lats), max(lats)
    west, east = min(lons), max(lons)
    # Buffer in gradi (approssimato) → poi trasformo nel CRS nativo
    dlat = EVENT_BUFFER_KM / 111.0
    dlon = EVENT_BUFFER_KM / (111.0 * math.cos(math.radians((south + north) / 2)))
    south -= dlat; north += dlat; west -= dlon; east += dlon
    try:
        if crs and not crs.is_geographic:
            from rasterio.warp import transform_bounds
            # da 4326 (i poligoni sono lat/lon) al CRS nativo del raster
            left, bottom, right, top = transform_bounds('EPSG:4326', crs,
                                                        west, south, east, north)
            return (left, bottom, right, top)
        return (west, south, east, north)
    except Exception:
        return None


def archive_event_frames(files, areas):
    """Per ogni area ARPA con evento attivo, archivia i frame recenti ritagliati
    (area + buffer) nella cartella evento. Idempotente: salta i frame già
    presenti. Aggiorna events_index.json."""
    active = active_storm_events()
    if not active:
        return 0
    try:
        from PIL import Image
        from rasterio.windows import from_bounds as _win_from_bounds
    except ImportError:
        log.warning('  Pillow/rasterio assente: skip archivio eventi')
        return 0

    parsed = []
    for f in files:
        ts = parse_timestamp(f)
        if ts:
            parsed.append((f, ts))
    parsed.sort(key=lambda x: x[1])
    recent = parsed[-FRAMES_KEEP:]   # ultimi frame disponibili nel bucket
    if not recent:
        return 0

    area_by_name = {a['name']: a for a in areas}
    total_written = 0

    for area_name, start_iso in active.items():
        area = area_by_name.get(area_name)
        if not area:
            continue
        event_id = f"{area_name}_{start_iso.replace(':', '').replace('-', '').replace('T', '_').rstrip('Z')}"
        ev_dir = EVENTS_DIR / event_id
        ev_dir.mkdir(parents=True, exist_ok=True)
        # index esistente (per append idempotente)
        idx_path = ev_dir / 'index.json'
        existing = {}
        if idx_path.exists():
            try:
                for e in json.loads(idx_path.read_text()).get('frames', []):
                    existing[e['file']] = e
            except Exception:
                pass

        for fname, ts in recent:
            ts_iso = ts.isoformat().replace('+00:00', 'Z')
            png_name = f"{ts.strftime('%y%m%d%H%M')}.png"
            if png_name in existing:
                continue   # già archiviato
            tiff = fetch_tiff(fname)
            if tiff is None:
                continue
            try:
                with MemoryFile(tiff) as mf:
                    with mf.open() as src:
                        crs = src.crs
                        bbox = _area_bbox_native(area, crs)
                        if not bbox:
                            continue
                        left, bottom, right, top = bbox
                        # Ritaglia: finestra dal bbox in coordinate native
                        try:
                            win = _win_from_bounds(left, bottom, right, top, src.transform)
                            arr = src.read(1, window=win).astype(np.float32)
                            wt = src.window_transform(win)
                            # bounds reali della finestra letta
                            from rasterio.transform import array_bounds
                            wb = array_bounds(arr.shape[0], arr.shape[1], wt)
                            wleft, wbottom, wright, wtop = wb
                        except Exception:
                            continue
                        nodata = src.nodata if src.nodata is not None else -9999
                        # Riproietta i bounds finestra a 4326 per Leaflet
                        if crs and not crs.is_geographic:
                            from rasterio.warp import transform_bounds
                            gl, gb, gr, gt = transform_bounds(crs, 'EPSG:4326',
                                                              wleft, wbottom, wright, wtop)
                        else:
                            gl, gb, gr, gt = wleft, wbottom, wright, wtop
                        if not (-180 <= gl <= 180 and -180 <= gr <= 180
                                and -90 <= gb <= 90 and -90 <= gt <= 90):
                            continue
                if arr.size == 0:
                    continue
                rgba = _dbz_to_rgba(arr, nodata)
                Image.fromarray(rgba, 'RGBA').save(ev_dir / png_name, optimize=True)
                existing[png_name] = {
                    'file': png_name, 'ts_utc': ts_iso,
                    'bounds': [[round(gb, 6), round(gl, 6)], [round(gt, 6), round(gr, 6)]],
                }
                total_written += 1
            except Exception as e:
                log.warning(f'  archivio frame {fname} ({area_name}) fallito: {e}')

        if existing:
            frames_sorted = sorted(existing.values(), key=lambda e: e['ts_utc'])
            idx_path.write_text(json.dumps({
                'event_id': event_id, 'area': area_name, 'start_utc': start_iso,
                'updated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                'frames': frames_sorted,
            }, indent=1))

    _rebuild_events_index()
    if total_written:
        log.info(f'✓ archivio eventi ARPA: {total_written} frame archiviati per {list(active)}')
    return total_written


def _rebuild_events_index():
    """Ricostruisce events_index.json elencando tutte le cartelle evento."""
    if not EVENTS_DIR.exists():
        return
    events = []
    for d in sorted(EVENTS_DIR.iterdir()):
        idx = d / 'index.json'
        if not d.is_dir() or not idx.exists():
            continue
        try:
            j = json.loads(idx.read_text())
            frames = j.get('frames', [])
            if not frames:
                continue
            events.append({
                'event_id': j.get('event_id', d.name),
                'area': j.get('area', ''),
                'start_utc': frames[0]['ts_utc'],
                'end_utc': frames[-1]['ts_utc'],
                'n_frames': len(frames),
            })
        except Exception:
            continue
    events.sort(key=lambda e: e['start_utc'], reverse=True)
    (FRAMES_DIR / 'events_index.json').write_text(json.dumps({
        'updated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'events': events,
    }, indent=1))


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

    # Frame PNG per la mappa (bypass 403 lato browser). Best-effort: un errore
    # qui non deve compromettere la raccolta stats (già committata sopra).
    try:
        save_radar_frames_png(files)
    except Exception as e:
        log.warning(f'save_radar_frames_png fallito: {e}')

    # Archivio eventi per replay: se c'è un evento "cella su area" attivo,
    # salva i frame ritagliati (area + 20km) in modo permanente. Best-effort.
    try:
        archive_event_frames(files, areas)
    except Exception as e:
        log.warning(f'archive_event_frames fallito: {e}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
