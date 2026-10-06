#!/usr/bin/env python3
"""
mch_collect.py — archivio del radar MeteoSwiss PRECIP (RZC, mm/h ogni 5') per area.

PERCHE'
  Terzo radar di STUDIO (nessuna allerta lo legge). La rete svizzera (5 radar
  doppia polarizzazione, in quota) guarda la Valtellina da NORD: a Cepina DPC e
  ARPA (che la guardano da sud) sono quasi ciechi. Da confrontare coi pluviometri
  (Cornalita, Oga) prima di qualunque uso operativo.
  Open data MeteoSwiss, CC BY 4.0 — "Fonte: MeteoSwiss". Lo scaricamento libero
  tiene solo gli ultimi 14 giorni: per avere uno storico bisogna archiviare noi.

DOMINIO
  Composito svizzero: Cepina e Ruspino vicini al bordo della copertura radar, Panna
  probabilmente fuori portata. Fuori griglia → status=out_of_domain; pixel senza dato
  → nodata_pct (quanta parte dell'area/anello non è coperta): mai 0 inventati.
  ATTENZIONE: se il file marca "fuori portata" come undetect (=0) e non come nodata,
  lontano dai radar si leggerebbero zeri falsi → verificare col --probe prima dell'uso.

COSA FA
  Elenca i file RZC dagli item STAC giornalieri (ch.meteoschweiz.ogd-radar-precip),
  scarica quelli non ancora archiviati, legge l'HDF5 ODIM e salva per area:
  max/mean nell'area, max e % sopra 5 mm/h nell'anello 0-RING_KM.
  La georeferenziazione si legge dal file (where/projdef + angolo UL): nessuna
  griglia scritta a mano.

OUTPUT
  archive/data/<area>_mch.csv            raccolta continua (ogni 10', da arpa-collect)
  archive/data/<area>_mch_backfill.csv   --backfill (ultimi 14 giorni disponibili)
  archive/data/radar_mch/<yymmddHHMM>.png + index.json   mappa live (pagina mch.html):
    ultimi MCH_PNG_KEEP frame, riproiettati in Web Mercator su Lombardia nord/Ticino/Grigioni;
    grigio = fuori copertura radar (nessun dato), trasparente = nessuna pioggia rilevata.
    timestamp_utc,area_name,max_mmh,mean_mmh,pixel_count,nodata_pct,
    ring_max_mmh,ring_wet5_pct,ring_nodata_pct,status,file,fetched_at_utc

USO
  python archive/scripts/mch_collect.py              # incrementale (ultime MCH_LOOKBACK_H ore)
  python archive/scripts/mch_collect.py --probe      # struttura di un file + stats aree (solo log)
  python archive/scripts/mch_collect.py --backfill   # tutto ciò che il STAC ha ancora (14 g)
Env: MCH_MAX_FILES (default 30 per run), MCH_LOOKBACK_H (default 3), MCH_RING_KM (default 10).
"""
import argparse
import csv
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STAC = 'https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-radar-precip/items'
BASE = Path(__file__).resolve().parent.parent
DATA = BASE / 'data'
AREAS_FILE = BASE / 'areas.json'
MAX_FILES = int(os.environ.get('MCH_MAX_FILES', '30'))
LOOKBACK_H = float(os.environ.get('MCH_LOOKBACK_H', '3'))
RING_KM = float(os.environ.get('MCH_RING_KM', '10'))
FIELDS = ['timestamp_utc', 'area_name', 'max_mmh', 'mean_mmh', 'pixel_count', 'nodata_pct',
          'ring_max_mmh', 'ring_wet5_pct', 'ring_nodata_pct', 'status', 'file', 'fetched_at_utc']
RZC_RE = re.compile(r'RZC(\d{2})(\d{3})(\d{2})(\d{2})', re.I)
UTC = timezone.utc
BACKFILL = False
PNG_DIR = DATA / 'radar_mch'
PNG_KEEP = int(os.environ.get('MCH_PNG_KEEP', '12'))
PNG_VIEW = (8.2, 45.25, 11.3, 47.25)          # lon/lat O,S,E,N: Ruspino, Cepina e i radar Lema/Weissfluh
PNG_RES_M = 500                                 # passo della griglia Web Mercator
# classi mm/h (stessa scala nella legenda di mch.html): (soglia, colore RGB)
PNG_CLASSES = [(0.1, (166, 216, 255)), (0.5, (95, 180, 245)), (1, (42, 120, 214)), (2, (25, 162, 107)),
               (5, (155, 209, 47)), (10, (245, 208, 0)), (20, (240, 140, 0)), (40, (224, 48, 48)),
               (70, (176, 22, 138))]

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('mch')
_s = requests.Session()
_s.headers['User-Agent'] = 'radar-dpc-mch-collect/1.0 (open data MeteoSwiss, CC BY 4.0)'


def iso(d):
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def ts_from_name(name):
    """RZCyyjjjHHMM… → datetime UTC (jjj = giorno dell'anno)."""
    m = RZC_RE.search(name)
    if not m:
        return None
    yy, jjj, hh, mm = (int(x) for x in m.groups())
    return datetime(2000 + yy, 1, 1, hh, mm, tzinfo=UTC) + timedelta(days=jjj - 1)


# ── STAC ─────────────────────────────────────────────────────────────────────
def list_rzc(since=None):
    """{datetime: href} dei file RZC negli item giornalieri (dal giorno di `since`)."""
    params = {'limit': 100}
    if since:
        params['datetime'] = since.strftime('%Y-%m-%dT00:00:00Z') + '/..'
    out, url = {}, STAC
    for _ in range(20):                                    # paginazione difensiva
        r = _s.get(url, params=params, timeout=60)
        if r.status_code == 400 and 'datetime' in params:  # filtro non accettato: lista tutto
            params.pop('datetime')
            continue
        r.raise_for_status()
        j = r.json()
        for feat in j.get('features', []):
            for key, a in (feat.get('assets') or {}).items():
                name = key or a.get('href', '')
                if not RZC_RE.search(name) and not RZC_RE.search(a.get('href', '')):
                    continue
                t = ts_from_name(name) or ts_from_name(a.get('href', ''))
                if t and (since is None or t >= since):
                    out[t] = a['href']
        nxt = next((l['href'] for l in j.get('links', []) if l.get('rel') == 'next'), None)
        if not nxt:
            break
        url, params = nxt, {}
    return out


# ── Lettura HDF5 ODIM e statistiche ──────────────────────────────────────────
def _attr(g, k, default=None):
    if g is None or k not in g.attrs:
        return default
    v = g.attrs[k]
    if hasattr(v, 'item') and getattr(v, 'size', 1) == 1:
        v = v.item()
    if isinstance(v, bytes):
        v = v.decode()
    return v


def read_grid(h5bytes):
    """→ dict(values float mm/h con NaN = nodata, crs, transform rasterio, quantity, meta)."""
    import h5py
    import numpy as np
    from pyproj import CRS, Transformer
    from rasterio.transform import from_origin
    with h5py.File(io.BytesIO(h5bytes), 'r') as f:
        where = f.get('where') or f.get('dataset1/where')
        what = f.get('dataset1/data1/what') or f.get('dataset1/what') or f.get('what')
        node = f.get('dataset1/data1/data') or f.get('dataset1/data')
        if where is None or node is None:
            raise ValueError('struttura ODIM inattesa (manca where o data)')
        raw = node[:]
        gain, offset = float(_attr(what, 'gain', 1.0)), float(_attr(what, 'offset', 0.0))
        nodata, undetect = _attr(what, 'nodata'), _attr(what, 'undetect')
        quantity = _attr(what, 'quantity', '?')
        projdef = _attr(where, 'projdef') or 'EPSG:2056'
        xscale, yscale = float(_attr(where, 'xscale')), float(_attr(where, 'yscale'))
        ul_lon, ul_lat = _attr(where, 'UL_lon'), _attr(where, 'UL_lat')
        meta = {k: _attr(where, k) for k in where.attrs}
    crs = CRS.from_user_input(projdef)
    if ul_lon is None or ul_lat is None:
        raise ValueError('angolo UL mancante: georeferenziazione non affidabile')
    ulx, uly = Transformer.from_crs('EPSG:4326', crs, always_xy=True).transform(float(ul_lon), float(ul_lat))
    vals = raw.astype('float64') * gain + offset
    bad = np.zeros(raw.shape, bool)
    if nodata is not None:
        bad |= raw == nodata
    vals = np.where(bad, np.nan, vals)
    if undetect is not None:
        vals = np.where((raw == undetect) & ~bad, 0.0, vals)   # undetect = nessuna pioggia rilevata
    vals = np.where(np.isfinite(vals), np.clip(vals, 0, None), np.nan)
    return {'v': vals, 'crs': crs, 'tr': from_origin(ulx, uly, xscale, yscale),
            'quantity': quantity, 'gain': gain, 'offset': offset, 'nodata': nodata, 'undetect': undetect,
            'shape': raw.shape, 'dtype': str(raw.dtype), 'meta': meta}


_GEOM = {}


def area_geoms(area, crs):
    """Poligono e anello (0-RING_KM dal bordo, area esclusa) nel CRS della griglia. Cache."""
    key = (area['name'], crs.to_string())
    if key not in _GEOM:
        from pyproj import Transformer
        from shapely.geometry import Polygon
        tr = Transformer.from_crs('EPSG:4326', crs, always_xy=True)
        poly = Polygon([tr.transform(lon, lat) for lat, lon in area['polygon']])
        _GEOM[key] = (poly, poly.buffer(RING_KM * 1000).difference(poly))
    return _GEOM[key]


def zone_stats(g, geom):
    import numpy as np
    from rasterio.features import geometry_mask
    from shapely.geometry import mapping
    inside = geometry_mask([mapping(geom)], out_shape=g['v'].shape, transform=g['tr'],
                           all_touched=True, invert=True)
    n = int(inside.sum())
    if n == 0:
        return None                                    # fuori dalla griglia
    vals = g['v'][inside]
    ok = vals[np.isfinite(vals)]
    nod = 100.0 * (n - ok.size) / n
    if ok.size == 0:
        return {'n': n, 'nodata': nod, 'max': None, 'mean': None, 'wet5': None}
    return {'n': n, 'nodata': nod, 'max': float(ok.max()), 'mean': float(ok.mean()),
            'wet5': float((ok >= 5).mean() * 100)}


def area_row(t, area, g, fname, fetched):
    f = lambda v, fmt: '' if v is None else fmt % v
    poly, ring = area_geoms(area, g['crs'])
    a, r = zone_stats(g, poly), zone_stats(g, ring)
    if a is None:
        status = 'out_of_domain'
    elif a['max'] is None:
        status = 'nodata'
    else:
        status = 'ok'
    return {'timestamp_utc': iso(t), 'area_name': area['name'],
            'max_mmh': f(a and a['max'], '%.2f'), 'mean_mmh': f(a and a['mean'], '%.3f'),
            'pixel_count': a['n'] if a else 0, 'nodata_pct': f(a and a['nodata'], '%.0f'),
            'ring_max_mmh': f(r and r['max'], '%.2f'), 'ring_wet5_pct': f(r and r['wet5'], '%.1f'),
            'ring_nodata_pct': f(r and r['nodata'], '%.0f') if r else '100',
            'status': status, 'file': fname, 'fetched_at_utc': fetched}


# ── Mappa live (PNG) ─────────────────────────────────────────────────────────
def render_png(g, t):
    """Frame PNG RGBA della vista PNG_VIEW in EPSG:3857 (allineato ai bounds di Leaflet)."""
    import numpy as np
    from PIL import Image
    from pyproj import Transformer
    from rasterio.crs import CRS as RCRS
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject
    W, S, E, N = PNG_VIEW
    tr = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)
    x0, y0 = tr.transform(W, S)
    x1, y1 = tr.transform(E, N)
    w, h = int((x1 - x0) / PNG_RES_M), int((y1 - y0) / PNG_RES_M)
    dst = np.full((h, w), np.nan)
    reproject(source=g['v'], destination=dst, src_transform=g['tr'], src_crs=RCRS.from_wkt(g['crs'].to_wkt()),
              dst_transform=from_origin(x0, y1, PNG_RES_M, PNG_RES_M), dst_crs='EPSG:3857',
              resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    rgba = np.zeros((h, w, 4), np.uint8)
    nan = ~np.isfinite(dst)
    rgba[nan] = (128, 128, 128, 70)                          # fuori copertura radar: grigio velato
    v = np.where(nan, 0, dst)
    for thr, rgb in PNG_CLASSES:
        m = v >= thr
        rgba[m] = (*rgb, 225)
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    name = t.strftime('%y%m%d%H%M') + '.png'
    Image.fromarray(rgba, 'RGBA').save(PNG_DIR / name, optimize=True)
    return name


def update_png_index():
    """Tiene gli ultimi PNG_KEEP frame e riscrive index.json (stesso schema di radar_arpa)."""
    if not PNG_DIR.exists():
        return
    files = sorted(p for p in PNG_DIR.glob('*.png'))
    for p in files[:-PNG_KEEP]:
        p.unlink()
    W, S, E, N = PNG_VIEW
    frames = [{'file': p.name,
               'ts_utc': iso(datetime.strptime(p.stem, '%y%m%d%H%M').replace(tzinfo=UTC)),
               'bounds': [[S, W], [N, E]]} for p in files[-PNG_KEEP:]]
    (PNG_DIR / 'index.json').write_text(json.dumps({
        'updated_at_utc': iso(datetime.now(UTC)), 'source': 'MeteoSwiss PRECIP (RZC), CC BY 4.0',
        'classes_mmh': [c for c, _ in PNG_CLASSES], 'frames': frames}, indent=1), encoding='utf-8')


# ── CSV ──────────────────────────────────────────────────────────────────────
def csv_path(area, backfill=None):
    bf = BACKFILL if backfill is None else backfill
    return DATA / (f'{area}_mch_backfill.csv' if bf else f'{area}_mch.csv')


def existing(area):
    out = set()
    for p in (csv_path(area, False), csv_path(area, True)):
        if p.exists():
            with open(p, newline='', encoding='utf-8') as fh:
                out |= {r['timestamp_utc'] for r in csv.DictReader(fh)}
    return out


def append(area, row):
    p = csv_path(area)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, 'a', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def load_areas():
    return [a for a in json.loads(AREAS_FILE.read_text(encoding='utf-8'))['areas'] if a.get('polygon')]


def download(href):
    r = _s.get(href, timeout=90)
    r.raise_for_status()
    if len(r.content) < 512:
        raise ValueError(f'file troppo piccolo ({len(r.content)} B)')
    return r.content


def report_progress(n_ok, remaining):
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as fh:
            fh.write(f'progress={n_ok}\nremaining={remaining}\n')


# ── Raccolta ─────────────────────────────────────────────────────────────────
def collect(files, areas, newest_first=False):
    have = {a['name']: existing(a['name']) for a in areas}
    todo = sorted((t for t in files if any(iso(t) not in have[a['name']] for a in areas)), reverse=newest_first)
    remaining = max(0, len(todo) - MAX_FILES)
    log.info(f'MeteoSwiss: {len(files)} file RZC nel STAC, {len(todo)} da archiviare (max {MAX_FILES} per run)')
    now = iso(datetime.now(UTC))
    n_ok = 0
    rendered = False
    pause = float(os.environ.get('MCH_PAUSE_S', '0.2' if BACKFILL else '0'))   # uso non eccessivo (condizioni MeteoSwiss)
    for t in todo[:MAX_FILES]:
        if pause:
            import time
            time.sleep(pause)
        href = files[t]
        fname = href.rsplit('/', 1)[-1]
        try:
            g = read_grid(download(href))
        except Exception as e:
            log.warning(f'  {iso(t)} {fname}: {e}')     # riprovato al prossimo run (non marcato)
            continue
        n_ok += 1
        if not BACKFILL and datetime.now(UTC) - t <= timedelta(hours=2):
            try:
                render_png(g, t)
                rendered = True
            except Exception as e:
                log.warning(f'  {iso(t)}: PNG non generato ({e})')
        for a in areas:
            if iso(t) not in have[a['name']]:
                try:
                    row = area_row(t, a, g, fname, now)
                except Exception as e:
                    log.warning(f'  {iso(t)} {a["name"]}: stats fallite {e}')
                    continue
                append(a['name'], row)
                have[a['name']].add(iso(t))
    if rendered:
        update_png_index()
    log.info(f'MeteoSwiss: {n_ok} file archiviati, restano {remaining}')
    report_progress(n_ok, remaining)
    return n_ok


def run_incremental():
    since = datetime.now(UTC) - timedelta(hours=LOOKBACK_H)
    collect(list_rzc(since), load_areas())


def run_backfill():
    global BACKFILL
    BACKFILL = True
    collect(list_rzc(None), load_areas(), newest_first=True)


def run_probe():
    """Stampa la struttura di un file recente e le stats per area: da incollare per la verifica."""
    import h5py
    files = list_rzc(datetime.now(UTC) - timedelta(hours=6))
    print(f'file RZC trovati nelle ultime 6 h: {len(files)}')
    if not files:
        return
    t = max(files)
    href = files[t]
    print(f'file più recente: {iso(t)}  {href}')
    b = download(href)
    print(f'dimensione: {len(b) // 1024} kB')
    with h5py.File(io.BytesIO(b), 'r') as f:
        def show(name, obj):
            attrs = {k: (v.decode() if isinstance(v, bytes) else (v.tolist() if hasattr(v, 'tolist') else v))
                     for k, v in obj.attrs.items()}
            kind = f'dataset {obj.shape} {obj.dtype}' if isinstance(obj, h5py.Dataset) else 'group'
            print(f'  /{name}: {kind} {attrs}')
        f.visititems(show)
    g = read_grid(b)
    import numpy as np
    v = g['v']
    print(f"quantity={g['quantity']} gain={g['gain']} offset={g['offset']} nodata={g['nodata']} undetect={g['undetect']}")
    print(f"griglia {g['shape']} {g['dtype']} · CRS {g['crs'].to_string()[:60]} · origine UL {g['tr'].c:.0f},{g['tr'].f:.0f}")
    print(f"valori: validi {np.isfinite(v).mean() * 100:.1f}% · max {np.nanmax(v):.2f} · pixel >0: {(v > 0).sum()}")
    for a in load_areas():
        print(' ', json.dumps(area_row(t, a, g, href.rsplit('/', 1)[-1], iso(datetime.now(UTC)))))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--probe', action='store_true', help='struttura file + stats aree (solo log)')
    ap.add_argument('--backfill', action='store_true', help='archivia tutto ciò che il STAC ha ancora (14 g)')
    a = ap.parse_args()
    try:
        if a.probe:
            run_probe()
        elif a.backfill:
            run_backfill()
        else:
            run_incremental()
    except Exception as e:
        log.error(f'mch_collect: {e}')
        sys.exit(1)
