#!/usr/bin/env python3
"""
alert_map.py — mappa radar per la pre-allerta "cella in avvicinamento" (solo Telegram).

Cosa disegna (1024x1024 px, EPSG:3857, finestra ~60x60 km centrata sull'area):
  - sfondo PRE-RENDERIZZATO una volta per area in archive/data/basemaps/<area>.jpg
    (+ .json con i bounds) dal workflow manuale basemaps.yml. Stili:
      osm  = OpenStreetMap standard, zoom 11 (nomi e strade leggibili)   [default]
      topo = OpenTopoMap, zoom 11 (rilievo e curve di livello)
    A ogni allerta NON si scarica nessuna tile. Se lo sfondo manca: fondo neutro;
  - pioggia (mm/h), stessa scala di colori del sito:
      radar DPC SRI (GeoTIFF già in memoria nel nowcast)  → `tiff_bytes`
      oppure un'altra griglia (es. radar MeteoSwiss)      → `grid=(valori, transform, crs_wkt)`
  - poligono dell'area + anelli a 5 e 10 km dal bordo;
  - freccia larga = moto d'insieme della perturbazione (nowcast.track_field_motion);
  - cerchio = pixel più intenso nell'anello; freccia sottile = moto della cella in 30'
    (nowcast.track_cell_motion), disegnata solo se coerente con la perturbazione.

Uso:
  render_alert_map(area, tiff_bytes, ...) -> bytes JPEG
  python alert_map.py --basemaps [osm|topo]   # genera gli sfondi (serve rete)
  python alert_map.py --demo <area> out.jpg   # anteprima senza pioggia
"""

import io
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

W = 1024                                  # lato immagine (px); Telegram mostra fino a 1280
U = W / 640                               # unità grafica: misure pensate per 640 px
HALF_GROUND_KM = 30                       # semi-lato della finestra al suolo
BASEMAP_ZOOM = 11
BASEMAP_DIR = Path(__file__).resolve().parent.parent / 'data' / 'basemaps'
STYLES = {
    'osm':  {'url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
             'attrib': '© OpenStreetMap contributors', 'color': 0.75, 'white': 0.12},
    'topo': {'url': 'https://tile.opentopomap.org/{z}/{x}/{y}.png',
             'attrib': '© OpenStreetMap contributors, SRTM · stile © OpenTopoMap (CC-BY-SA)',
             'color': 0.65, 'white': 0.15},
}
TILE_UA = 'radar-dpc-basemap/1.1 (github.com/EbbeneTriglav/radar-dpc; one-off area basemaps)'
# stessa scala del sito (mch_collect.PNG_CLASSES)
CLASSES = [(0.1, (166, 216, 255)), (0.5, (95, 180, 245)), (1, (42, 120, 214)), (2, (25, 162, 107)),
           (5, (155, 209, 47)), (10, (245, 208, 0)), (20, (240, 140, 0)), (40, (224, 48, 48)),
           (70, (176, 22, 138))]
R_EARTH = 6378137.0
COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def u(v):
    """Misura grafica scalata alla dimensione dell'immagine."""
    return int(round(v * U))


def _font(size, bold=False):
    from PIL import ImageFont
    size = u(size)
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
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _basemap_meta(area):
    j = BASEMAP_DIR / f"{area['name']}.json"
    if j.exists():
        try:
            return json.loads(j.read_text())
        except Exception:
            pass
    return {}


def view_bounds(area):
    """Bounds EPSG:3857 (x0, y0, x1, y1). Se c'è lo sfondo, usa i SUOI bounds."""
    b = _basemap_meta(area).get('bounds_3857')
    if b:
        return tuple(b)
    lat, lon = _centroid(area)
    cx, cy = _merc(lon, lat)
    half = HALF_GROUND_KM * 1000 / math.cos(math.radians(lat))   # metri Mercator
    return (cx - half, cy - half, cx + half, cy + half)


# ── Sfondo (una tantum) ──────────────────────────────────────────────────────
def make_basemap(area, fetch=None, z=BASEMAP_ZOOM, style='osm'):
    """Scarica le tile che coprono la finestra, le cuce, ritaglia, attenua appena i colori.
    `fetch(url) -> bytes` iniettabile per i test."""
    from PIL import Image, ImageEnhance
    st = STYLES[style]
    if fetch is None:
        import requests
        sess = requests.Session()
        sess.headers['User-Agent'] = TILE_UA

        def fetch(url):
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            return r.content
    lat, lon = _centroid(area)
    cx, cy = _merc(lon, lat)
    half = HALF_GROUND_KM * 1000 / math.cos(math.radians(lat))
    x0, y0, x1, y1 = cx - half, cy - half, cx + half, cy + half
    tile_m = 2 * math.pi * R_EARTH / 2 ** z
    org = math.pi * R_EARTH
    tx0, tx1 = int((x0 + org) // tile_m), int((x1 + org) // tile_m)
    ty0, ty1 = int((org - y1) // tile_m), int((org - y0) // tile_m)
    mosaic = Image.new('RGB', ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (240, 240, 240))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            img = Image.open(io.BytesIO(fetch(st['url'].format(z=z, x=tx, y=ty)))).convert('RGB')
            mosaic.paste(img, ((tx - tx0) * 256, (ty - ty0) * 256))
            time.sleep(0.25)                                  # cortesia verso i server delle tile
    pxm = 256 / tile_m
    left, top = (x0 + org - tx0 * tile_m) * pxm, (org - y1 - ty0 * tile_m) * pxm
    crop = mosaic.crop((round(left), round(top), round(left + 2 * half * pxm), round(top + 2 * half * pxm)))
    crop = crop.resize((W, W), Image.LANCZOS)
    crop = ImageEnhance.Color(crop).enhance(st['color'])     # colori un po' attenuati: la pioggia risalta
    crop = Image.blend(crop, Image.new('RGB', crop.size, (255, 255, 255)), st['white'])
    BASEMAP_DIR.mkdir(parents=True, exist_ok=True)
    crop.save(BASEMAP_DIR / f"{area['name']}.jpg", 'JPEG', quality=90, optimize=True)
    (BASEMAP_DIR / f"{area['name']}.json").write_text(json.dumps({
        'bounds_3857': [x0, y0, x1, y1], 'zoom': z, 'size_px': W, 'half_ground_km': HALF_GROUND_KM,
        'style': style, 'attrib': st['attrib']}, indent=1))
    return crop


def _basemap(area):
    from PIL import Image
    p = BASEMAP_DIR / f"{area['name']}.jpg"
    if p.exists():
        try:
            img = Image.open(p).convert('RGB')
            if img.size != (W, W):
                img = img.resize((W, W), Image.LANCZOS)
            return img, _basemap_meta(area).get('attrib', '© OpenStreetMap contributors')
        except Exception:
            pass
    return Image.new('RGB', (W, W), (238, 238, 234)), None


# ── Pioggia ──────────────────────────────────────────────────────────────────
def _reproject(arr, transform, crs, bounds):
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject
    dst = np.full((W, W), np.nan, dtype='float32')
    reproject(source=arr.astype('float32'), destination=dst, src_transform=transform, src_crs=crs,
              dst_transform=from_bounds(*bounds, W, W), dst_crs='EPSG:3857',
              resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    return dst


def _rain_grid(tiff_bytes, bounds):
    import rasterio
    with rasterio.open(io.BytesIO(tiff_bytes)) as src:
        a = src.read(1).astype('float32')
        nd = src.nodata if src.nodata is not None else -9999
        a[(a == nd) | ~np.isfinite(a) | (a < -900) | (a > 10000)] = np.nan
        return _reproject(a, src.transform, src.crs, bounds)


def _rain_rgba(grid):
    rgba = np.zeros((W, W, 4), np.uint8)
    v = np.where(np.isfinite(grid), grid, 0)
    for thr, rgb in CLASSES:                 # pioggia debole più trasparente: si legge lo sfondo
        rgba[v >= thr] = (*rgb, 110 if thr < 1 else 165 if thr < 5 else 215)
    return rgba


# ── Disegno ──────────────────────────────────────────────────────────────────
def _from_dir(bearing):
    """Direzione di PROVENIENZA (opposta al moto), es. moto verso NE → 'da SW'."""
    return COMPASS[round(((bearing + 180) % 360) / 45) % 8]


def _big_arrow(d, tail, head, fill=(25, 45, 110, 205), outline=(255, 255, 255, 235)):
    """Freccia larga piena (poligono) da tail a head."""
    width, head_w, head_l = u(16), u(44), u(38)
    (x0, y0), (x1, y1) = tail, head
    L = math.hypot(x1 - x0, y1 - y0)
    if L < head_l + u(10):
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    nx, ny = -uy, ux
    bx, by = x1 - ux * head_l, y1 - uy * head_l
    w, hw = width / 2, head_w / 2
    poly = [(x0 + nx * w, y0 + ny * w), (bx + nx * w, by + ny * w), (bx + nx * hw, by + ny * hw), (x1, y1),
            (bx - nx * hw, by - ny * hw), (bx - nx * w, by - ny * w), (x0 - nx * w, y0 - ny * w)]
    d.polygon(poly, fill=fill, outline=outline, width=u(3))


def _label(d, xy, text, font, fg=(0, 0, 0, 255), bg=(255, 255, 255, 220)):
    x, y = xy
    tw = d.textlength(text, font=font)
    x = min(max(x, u(4)), W - tw - u(6))
    y = min(max(y, u(34)), W - u(72))
    d.rectangle([x - u(4), y - u(3), x + tw + u(4), y + font.size + u(4)], fill=bg)
    d.text((x, y), text, fill=fg, font=font)


def render_alert_map(area, tiff_bytes, ts_label='', signal=None, motion=None,
                     buffers_km=(5, 10), title='', quality=80, field_motion=None,
                     grid=None, source='Radar DPC SRI · moti stimati'):
    """JPEG della mappa. Solleva eccezioni: il chiamante le intercetta e manda solo testo.
    `grid` = (array mm/h con NaN, transform rasterio, crs) in alternativa a `tiff_bytes`."""
    from PIL import Image, ImageDraw
    from shapely.geometry import Polygon
    bounds = view_bounds(area)
    x0, y0, x1, y1 = bounds
    s = W / (x1 - x0)

    def px(x, y):
        return ((x - x0) * s, (y1 - y) * s)

    lat_c, _ = _centroid(area)
    k = 1 / math.cos(math.radians(lat_c))                     # metri al suolo -> metri Mercator

    img, bg_attrib = _basemap(area)
    img = img.convert('RGBA')
    rain = None
    if grid is not None:
        rain = _reproject(np.asarray(grid[0]), grid[1], grid[2], bounds)
    elif tiff_bytes:
        rain = _rain_grid(tiff_bytes, bounds)
    if rain is not None:
        img.alpha_composite(Image.fromarray(_rain_rgba(rain), 'RGBA'))
    d = ImageDraw.Draw(img, 'RGBA')

    poly_m = Polygon([_merc(lon, lat) for lat, lon in area['polygon']])
    for km in sorted(buffers_km, reverse=True):
        ring = poly_m.buffer(km * 1000 * k).exterior.coords
        pts = [px(*c) for c in ring]
        for i in range(0, len(pts) - 1, 2):                   # tratteggio
            d.line([pts[i], pts[i + 1]], fill=(20, 20, 20, 210), width=u(2))
        lx, ly = px(*ring[0])
        _label(d, (lx + u(4), ly - u(16)), f'{km} km', _font(12), bg=(255, 255, 255, 170))
    d.polygon([px(*c) for c in poly_m.exterior.coords], outline=(0, 0, 0, 255), width=u(3))

    # perturbazione: freccia larga che ENTRA dal lato di provenienza e punta verso l'area
    if field_motion and field_motion.get('bearing_deg') is not None:
        b = math.radians(field_motion['bearing_deg'])
        ux, uy = math.sin(b), -math.cos(b)                    # verso del moto in pixel (y verso il basso)
        acx, acy = px(*poly_m.centroid.coords[0])
        r_ring = max(math.hypot(px(*c)[0] - acx, px(*c)[1] - acy)
                     for c in poly_m.buffer(max(buffers_km) * 1000 * k).exterior.coords)
        head = (acx - ux * (r_ring + u(14)), acy - uy * (r_ring + u(14)))
        tail = (head[0] - ux * u(150), head[1] - uy * u(150))
        m = u(40)                                             # coda dentro il riquadro
        tail = (min(max(tail[0], m), W - m), min(max(tail[1], u(40) + m), W - u(50) - m))
        _big_arrow(d, tail, head)
        flab = (f"Perturbazione da {_from_dir(field_motion['bearing_deg'])} · "
                f"{field_motion['speed_kmh']:.0f} km/h")
        fb = _font(14, True)
        tw = d.textlength(flab, font=fb)
        ty = tail[1] + u(16) if tail[1] < W / 2 else tail[1] - u(36)
        _label(d, (tail[0] - tw / 2, ty), flab, fb, fg=(255, 255, 255, 255), bg=(25, 45, 110, 225))
    elif field_motion and field_motion.get('compass') == 'stazionaria':
        _label(d, (u(8), u(36)), 'Perturbazione quasi ferma', _font(13, True),
               fg=(255, 255, 255, 255), bg=(25, 45, 110, 225))

    # cella + freccia di moto stimato (30'), solo se il moto della cella è affidabile
    if signal and signal.get('max_lat') is not None:
        cxm, cym = _merc(signal['max_lon'], signal['max_lat'])
        cx, cy = px(cxm, cym)
        r = u(9)
        d.ellipse([cx - r - u(2), cy - r - u(2), cx + r + u(2), cy + r + u(2)], outline=(255, 255, 255, 230), width=u(3))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 0, 0, 255), width=u(3))
        if motion and motion.get('bearing_deg') is not None and motion.get('speed_kmh'):
            L_km = max(4.0, min(20.0, motion['speed_kmh'] * 0.5))
            b = math.radians(motion['bearing_deg'])
            ex, ey = px(cxm + math.sin(b) * L_km * 1000 * k, cym + math.cos(b) * L_km * 1000 * k)
            for col, wdt in (((255, 255, 255, 230), u(9)), ((0, 0, 0, 255), u(5))):
                d.line([(cx, cy), (ex, ey)], fill=col, width=wdt)
                for t in (150, -150):
                    rr = b + math.radians(t)
                    d.line([(ex, ey), (ex + u(18) * math.sin(rr), ey - u(18) * math.cos(rr))], fill=col, width=wdt)
            _label(d, (ex + u(8), ey - u(8)), f"cella → {motion.get('compass', '')} {motion['speed_kmh']:.0f} km/h",
                   _font(13, True))
        elif motion and motion.get('compass') == 'stazionaria':
            _label(d, (cx + u(14), cy - u(8)), 'cella ferma', _font(13, True))

    # barra di scala 10 km
    sb = 10 * 1000 * k * s
    d.rectangle([u(12), W - u(64), u(12) + sb, W - u(58)], fill=(0, 0, 0, 230))
    _label(d, (u(12), W - u(82)), '10 km', _font(12), bg=(255, 255, 255, 170))

    # testata
    d.rectangle([0, 0, W, u(28)], fill=(255, 255, 255, 230))
    d.text((u(8), u(6)), title or area.get('label', area['name']), fill=(0, 0, 0, 255), font=_font(14, True))
    if ts_label:
        f = _font(13)
        d.text((W - d.textlength(ts_label, font=f) - u(8), u(7)), ts_label, fill=(40, 40, 40, 255), font=f)

    # legenda + fonti
    d.rectangle([0, W - u(44), W, W], fill=(255, 255, 255, 235))
    fx = u(8)
    f10 = _font(11)
    d.text((fx, W - u(41)), 'mm/h', fill=(0, 0, 0, 255), font=f10)
    fx += u(36)
    for thr, rgb in CLASSES:
        d.rectangle([fx, W - u(40), fx + u(14), W - u(28)], fill=(*rgb, 255))
        lab = f'{thr:g}'
        d.text((fx + u(17), W - u(41)), lab, fill=(0, 0, 0, 255), font=f10)
        fx += u(22) + d.textlength(lab, font=f10)
    attrib = f"{source} · mappa {bg_attrib}" if bg_attrib else f"{source} · sfondo non disponibile"
    fa = f10
    for sz in (11, 10, 9, 8):                                # rimpicciolisce se non ci sta
        fa = _font(sz)
        if d.textlength(attrib, font=fa) <= W - u(16):
            break
    d.text((u(8), W - u(20)), attrib, fill=(60, 60, 60, 255), font=fa)

    out = io.BytesIO()
    img.convert('RGB').save(out, 'JPEG', quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def _load_areas():
    p = Path(__file__).resolve().parent.parent / 'areas.json'
    d = json.loads(p.read_text())
    return d.get('areas', d) if isinstance(d, dict) else d


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--basemaps':
        style = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in STYLES else 'osm'
        ok = 0
        for a in _load_areas():
            try:
                make_basemap(a, style=style)
                print(f"sfondo {a['name']} ({style}): ok")
                ok += 1
            except Exception as e:
                print(f"sfondo {a['name']} ({style}): ERRORE {e}")
        sys.exit(0 if ok else 1)
    if len(sys.argv) > 3 and sys.argv[1] == '--demo':
        a = next(x for x in _load_areas() if x['name'] == sys.argv[2])
        jpg = render_alert_map(a, None, 'anteprima', None, None,
                               title=f"{a.get('label', a['name'])} · anteprima senza pioggia")
        Path(sys.argv[3]).write_bytes(jpg)
        print(f'{sys.argv[3]}: {len(jpg) / 1024:.0f} KB')
