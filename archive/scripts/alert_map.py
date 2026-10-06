#!/usr/bin/env python3
"""
alert_map.py — mappa radar per la pre-allerta "cella in avvicinamento" (solo Telegram).

Cosa disegna (640x640 px, EPSG:3857, finestra ~60x60 km centrata sull'area):
  - sfondo: mappa OpenStreetMap schiarita, PRE-RENDERIZZATA una volta per area in
    archive/data/basemaps/<area>.jpg (+ .json con i bounds), generata dal workflow
    manuale basemaps.yml. A ogni allerta NON si scarica nulla dall'esterno.
    Se lo sfondo manca, la mappa esce su fondo neutro (mai bloccare l'avviso);
  - pioggia: frame radar DPC SRI (mm/h) gia' in memoria nel nowcast, stessa scala
    di colori del sito (pagina MeteoSwiss);
  - poligono dell'area + anelli a 5 e 10 km dal bordo;
  - cella: cerchio sul pixel massimo nell'anello; freccia = spostamento STIMATO in
    30 minuti (baricentro di 2 frame a 5'), con direzione e velocita'.

Uso:
  render_alert_map(area, tiff_bytes, ...) -> bytes JPEG (o None se fallisce)
  python alert_map.py --basemaps            # genera gli sfondi (serve rete OSM)
  python alert_map.py --demo <area> out.jpg # prova con pioggia sintetica
"""

import io
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

W = 640                                   # lato immagine (px)
HALF_GROUND_KM = 30                       # semi-lato della finestra al suolo
BASEMAP_ZOOM = 10
BASEMAP_DIR = Path(__file__).resolve().parent.parent / 'data' / 'basemaps'
OSM_TILE = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
OSM_UA = 'radar-dpc-basemap/1.0 (github.com/EbbeneTriglav/radar-dpc; one-off area basemaps)'
ATTRIB = 'Radar DPC SRI · moti stimati · mappa © OpenStreetMap contributors'
# stessa scala del sito (mch_collect.PNG_CLASSES)
CLASSES = [(0.1, (166, 216, 255)), (0.5, (95, 180, 245)), (1, (42, 120, 214)), (2, (25, 162, 107)),
           (5, (155, 209, 47)), (10, (245, 208, 0)), (20, (240, 140, 0)), (40, (224, 48, 48)),
           (70, (176, 22, 138))]
R_EARTH = 6378137.0


def _font(size, bold=False):
    from PIL import ImageFont
    for f in (('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'),
              ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
               else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _merc(lon, lat):
    x = math.radians(lon) * R_EARTH
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R_EARTH
    return x, y


def _centroid(area):
    pts = area['polygon']                                  # [[lat, lon], ...]
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    return lat, lon


def view_bounds(area):
    """Bounds EPSG:3857 (x0, y0, x1, y1). Se c'e' lo sfondo, usa i SUOI bounds."""
    j = BASEMAP_DIR / f"{area['name']}.json"
    if j.exists():
        try:
            return tuple(json.loads(j.read_text())['bounds_3857'])
        except Exception:
            pass
    lat, lon = _centroid(area)
    cx, cy = _merc(lon, lat)
    half = HALF_GROUND_KM * 1000 / math.cos(math.radians(lat))   # metri Mercator
    return (cx - half, cy - half, cx + half, cy + half)


# ── Sfondo (una tantum) ──────────────────────────────────────────────────────
def make_basemap(area, fetch=None, z=BASEMAP_ZOOM):
    """Scarica le tile OSM che coprono la finestra, le cuce, ritaglia, schiarisce.
    `fetch(url) -> bytes` iniettabile per i test."""
    from PIL import Image, ImageEnhance
    if fetch is None:
        import requests
        s = requests.Session()
        s.headers['User-Agent'] = OSM_UA

        def fetch(url):
            r = s.get(url, timeout=30)
            r.raise_for_status()
            return r.content
    lat, lon = _centroid(area)
    cx, cy = _merc(lon, lat)
    half = HALF_GROUND_KM * 1000 / math.cos(math.radians(lat))
    x0, y0, x1, y1 = cx - half, cy - half, cx + half, cy + half
    n = 2 ** z
    tile_m = 2 * math.pi * R_EARTH / n
    org = math.pi * R_EARTH
    tx0, tx1 = int((x0 + org) // tile_m), int((x1 + org) // tile_m)
    ty0, ty1 = int((org - y1) // tile_m), int((org - y0) // tile_m)
    mosaic = Image.new('RGB', ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (240, 240, 240))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            img = Image.open(io.BytesIO(fetch(OSM_TILE.format(z=z, x=tx, y=ty)))).convert('RGB')
            mosaic.paste(img, ((tx - tx0) * 256, (ty - ty0) * 256))
            time.sleep(0.2)                                   # cortesia verso i server OSM
    px = 256 / tile_m
    left, top = (x0 + org - tx0 * tile_m) * px, (org - y1 - ty0 * tile_m) * px
    crop = mosaic.crop((round(left), round(top), round(left + 2 * half * px), round(top + 2 * half * px)))
    crop = crop.resize((W, W), Image.LANCZOS)
    crop = ImageEnhance.Color(crop).enhance(0.45)            # meno saturo: la pioggia deve risaltare
    crop = Image.blend(crop, Image.new('RGB', crop.size, (255, 255, 255)), 0.35)
    BASEMAP_DIR.mkdir(parents=True, exist_ok=True)
    crop.save(BASEMAP_DIR / f"{area['name']}.jpg", 'JPEG', quality=88, optimize=True)
    (BASEMAP_DIR / f"{area['name']}.json").write_text(json.dumps({
        'bounds_3857': [x0, y0, x1, y1], 'zoom': z, 'size_px': W, 'half_ground_km': HALF_GROUND_KM,
        'source': 'OpenStreetMap tiles (© OpenStreetMap contributors, ODbL)'}, indent=1))
    return crop


def _basemap(area):
    from PIL import Image
    p = BASEMAP_DIR / f"{area['name']}.jpg"
    if p.exists():
        try:
            return Image.open(p).convert('RGB').resize((W, W)), True
        except Exception:
            pass
    return Image.new('RGB', (W, W), (238, 238, 234)), False


# ── Pioggia ──────────────────────────────────────────────────────────────────
def _rain_grid(tiff_bytes, bounds):
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject
    dst = np.full((W, W), np.nan, dtype='float32')
    with rasterio.open(io.BytesIO(tiff_bytes)) as src:
        a = src.read(1).astype('float32')
        nd = src.nodata if src.nodata is not None else -9999
        a[(a == nd) | ~np.isfinite(a) | (a < -900) | (a > 10000)] = np.nan
        reproject(source=a, destination=dst, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=from_bounds(*bounds, W, W), dst_crs='EPSG:3857',
                  resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    return dst


def _rain_rgba(grid):
    rgba = np.zeros((W, W, 4), np.uint8)
    v = np.where(np.isfinite(grid), grid, 0)
    for thr, rgb in CLASSES:                 # pioggia debole più trasparente: si legge lo sfondo
        rgba[v >= thr] = (*rgb, 120 if thr < 1 else 170 if thr < 5 else 215)
    return rgba


# ── Disegno ──────────────────────────────────────────────────────────────────
COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def _from_dir(bearing):
    """Direzione di PROVENIENZA (opposta al moto), es. moto verso NE → 'da SW'."""
    return COMPASS[round(((bearing + 180) % 360) / 45) % 8]


def _big_arrow(d, tail, head, width=16, head_w=44, head_l=38, fill=(25, 45, 110, 205),
               outline=(255, 255, 255, 235)):
    """Freccia larga piena (poligono) da tail a head."""
    (x0, y0), (x1, y1) = tail, head
    L = math.hypot(x1 - x0, y1 - y0)
    if L < head_l + 10:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    nx, ny = -uy, ux
    bx, by = x1 - ux * head_l, y1 - uy * head_l
    w, hw = width / 2, head_w / 2
    poly = [(x0 + nx * w, y0 + ny * w), (bx + nx * w, by + ny * w), (bx + nx * hw, by + ny * hw), (x1, y1),
            (bx - nx * hw, by - ny * hw), (bx - nx * w, by - ny * w), (x0 - nx * w, y0 - ny * w)]
    d.polygon(poly, fill=fill, outline=outline, width=3)


def render_alert_map(area, tiff_bytes, ts_label='', signal=None, motion=None,
                     buffers_km=(5, 10), title='', quality=82, field_motion=None):
    """JPEG della mappa. Solleva eccezioni: il chiamante le intercetta e manda solo testo."""
    from PIL import Image, ImageDraw
    from shapely.geometry import Polygon
    bounds = view_bounds(area)
    x0, y0, x1, y1 = bounds
    s = W / (x1 - x0)

    def px(x, y):
        return ((x - x0) * s, (y1 - y) * s)

    lat_c, _ = _centroid(area)
    k = 1 / math.cos(math.radians(lat_c))                     # metri al suolo -> metri Mercator

    img, has_bg = _basemap(area)
    img = img.convert('RGBA')
    if tiff_bytes:
        img.alpha_composite(Image.fromarray(_rain_rgba(_rain_grid(tiff_bytes, bounds)), 'RGBA'))
    d = ImageDraw.Draw(img, 'RGBA')

    poly_m = Polygon([_merc(lon, lat) for lat, lon in area['polygon']])
    for km in sorted(buffers_km, reverse=True):
        ring = poly_m.buffer(km * 1000 * k).exterior.coords
        pts = [px(*c) for c in ring]
        for i in range(0, len(pts) - 1, 2):                   # tratteggio
            d.line([pts[i], pts[i + 1]], fill=(30, 30, 30, 200), width=2)
        lx, ly = px(*ring[0])
        d.text((lx + 3, ly - 14), f'{km} km', fill=(30, 30, 30, 255), font=_font(12))
    d.polygon([px(*c) for c in poly_m.exterior.coords], outline=(0, 0, 0, 255), width=3)

    # perturbazione nel suo insieme: freccia larga che ENTRA dal lato di provenienza e punta
    # verso l'area, fermandosi fuori dall'anello esterno
    if field_motion and field_motion.get('bearing_deg') is not None:
        b = math.radians(field_motion['bearing_deg'])
        ux, uy = math.sin(b), -math.cos(b)                    # verso del moto in pixel (y verso il basso)
        acx, acy = px(*poly_m.centroid.coords[0])
        r_ring = max(math.hypot(px(*c)[0] - acx, px(*c)[1] - acy)
                     for c in poly_m.buffer(max(buffers_km) * 1000 * k).exterior.coords)
        head = (acx - ux * (r_ring + 14), acy - uy * (r_ring + 14))
        tail = (head[0] - ux * 150, head[1] - uy * 150)
        m = 40                                                # tiene la coda dentro il riquadro
        tail = (min(max(tail[0], m), W - m), min(max(tail[1], 40 + m), W - 50 - m))
        _big_arrow(d, tail, head)
        flab = (f"Perturbazione da {_from_dir(field_motion['bearing_deg'])} · "
                f"{field_motion['speed_kmh']:.0f} km/h")
        fb = _font(14, True)
        tw = d.textlength(flab, font=fb)
        tx = min(max(tail[0] - tw / 2, 6), W - tw - 8)
        ty = tail[1] + 14 if tail[1] < W / 2 else tail[1] - 34
        ty = min(max(ty, 34), W - 72)
        d.rectangle([tx - 4, ty - 3, tx + tw + 4, ty + 18], fill=(25, 45, 110, 225))
        d.text((tx, ty), flab, fill=(255, 255, 255, 255), font=fb)
    elif field_motion and field_motion.get('compass') == 'stazionaria':
        fb = _font(13, True)
        d.rectangle([6, 34, 14 + d.textlength('Perturbazione quasi ferma', font=fb), 54], fill=(25, 45, 110, 225))
        d.text((10, 36), 'Perturbazione quasi ferma', fill=(255, 255, 255, 255), font=fb)

    # cella + freccia di moto stimato (30')
    if signal and signal.get('max_lat') is not None:
        cxm, cym = _merc(signal['max_lon'], signal['max_lat'])
        cx, cy = px(cxm, cym)
        d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], outline=(0, 0, 0, 255), width=3)
        if motion and motion.get('bearing_deg') is not None and motion.get('speed_kmh'):
            L_km = max(4.0, min(25.0, motion['speed_kmh'] * 0.5))
            b = math.radians(motion['bearing_deg'])
            ex, ey = px(cxm + math.sin(b) * L_km * 1000 * k, cym + math.cos(b) * L_km * 1000 * k)
            for col, wdt in (((255, 255, 255, 230), 9), ((0, 0, 0, 255), 5)):
                d.line([(cx, cy), (ex, ey)], fill=col, width=wdt)
                for t in (150, -150):
                    r = b + math.radians(t)
                    d.line([(ex, ey), (ex + 18 * math.sin(r), ey - 18 * math.cos(r))], fill=col, width=wdt)
            lab = f"cella → {motion.get('compass', '')} {motion['speed_kmh']:.0f} km/h"
            tx, ty = ex + 8, ey - 8
            f12 = _font(13, True)
            tw = d.textlength(lab, font=f12)
            tx = min(max(tx, 4), W - tw - 6)
            ty = min(max(ty, 34), W - 70)
            d.rectangle([tx - 3, ty - 2, tx + tw + 3, ty + 16], fill=(255, 255, 255, 215))
            d.text((tx, ty), lab, fill=(0, 0, 0, 255), font=f12)
        elif motion and motion.get('compass') == 'stazionaria':
            d.text((cx + 12, cy - 8), 'cella ferma', fill=(0, 0, 0, 255), font=_font(13, True))

    # barra di scala 10 km
    sb = 10 * 1000 * k * s
    d.rectangle([12, W - 64, 12 + sb, W - 58], fill=(0, 0, 0, 230))
    d.text((12, W - 80), '10 km', fill=(0, 0, 0, 255), font=_font(12))

    # testata
    d.rectangle([0, 0, W, 28], fill=(255, 255, 255, 225))
    d.text((8, 6), title or area.get('label', area['name']), fill=(0, 0, 0, 255), font=_font(14, True))
    if ts_label:
        f = _font(13)
        d.text((W - d.textlength(ts_label, font=f) - 8, 7), ts_label, fill=(40, 40, 40, 255), font=f)

    # legenda + fonte
    d.rectangle([0, W - 44, W, W], fill=(255, 255, 255, 230))
    fx = 8
    f10 = _font(11)
    d.text((fx, W - 41), 'mm/h', fill=(0, 0, 0, 255), font=f10)
    fx += 36
    for thr, rgb in CLASSES:
        d.rectangle([fx, W - 40, fx + 14, W - 28], fill=(*rgb, 255))
        lab = f'{thr:g}'
        d.text((fx + 17, W - 41), lab, fill=(0, 0, 0, 255), font=f10)
        fx += 22 + d.textlength(lab, font=f10)
    attrib = ATTRIB if has_bg else 'Radar DPC SRI · sfondo non disponibile'
    d.text((8, W - 20), attrib, fill=(60, 60, 60, 255), font=f10)

    out = io.BytesIO()
    img.convert('RGB').save(out, 'JPEG', quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def _load_areas():
    p = Path(__file__).resolve().parent.parent / 'areas.json'
    d = json.loads(p.read_text())
    return d.get('areas', d) if isinstance(d, dict) else d


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--basemaps':
        ok = 0
        for a in _load_areas():
            try:
                make_basemap(a)
                print(f"sfondo {a['name']}: ok")
                ok += 1
            except Exception as e:
                print(f"sfondo {a['name']}: ERRORE {e}")
        sys.exit(0 if ok else 1)
    if len(sys.argv) > 3 and sys.argv[1] == '--demo':
        a = next(x for x in _load_areas() if x['name'] == sys.argv[2])
        jpg = render_alert_map(a, None, '14:05', None, None, title=f"{a.get('label', a['name'])} · demo senza pioggia")
        Path(sys.argv[3]).write_bytes(jpg)
        print(f'{sys.argv[3]}: {len(jpg) / 1024:.0f} KB')
