#!/usr/bin/env python3
"""
reconstruct_events.py — recupero eventi "cella su area" NON registrati.

Quando lo scheduler GitHub Actions si inceppa (i cron sono best-effort), il
nowcast può non girare durante un temporale: l'evento accade ma non viene
scritto in events.csv, quindi non compare nelle tabelle/pagine che leggono da lì.

Questo script ripara la situazione a posteriori: legge i dati radar GREZZI già
archiviati (ARPA per Ruspino/Cepina, CUM3 DPC per tutte le aree), rileva i
picchi sopra soglia che NON hanno un evento corrispondente in events.csv, e li
scrive come COPPIE COMPLETE storm_on_area + storm_cleared, marcate 'ricostruito'.

Perché coppie complete e non solo l'apertura: arpa_collect.active_storm_events()
considera "attivo" un evento con storm_on_area senza storm_cleared successivo.
Scrivere solo l'apertura di un evento passato lo farebbe sembrare attivo ORA,
con archiviazione frame errata. La coppia completa evita ogni ambiguità e
coincide con la logica del nowcast: nessuno script si confonde.

Idempotente: se un evento (reale o già ricostruito) esiste entro ±3h, salta.
Uso:
  python reconstruct_events.py            # ricostruisce dagli ultimi dati
  python reconstruct_events.py --dry-run  # mostra cosa farebbe, non scrive
"""
from __future__ import annotations
import argparse
import csv
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
EVENTS_CSV = DATA / 'events.csv'

ARPA_AREAS = {'ruspino', 'cepina'}
ALL_AREAS = ['ruspino', 'cepina', 'panna']
RECON_THRESHOLD_MMH = 10.0     # allineata alla soglia SRI warning del nowcast
GAP_MIN = 90                   # gap che separa due eventi distinti (minuti)
DEDUP_H = 3                    # tolleranza anti-duplicato con eventi esistenti (ore)

EVENTS_HEADER = [
    'event_timestamp_utc', 'area_name', 'level', 'threshold_mm',
    'observed_mm_mean', 'observed_mm_max', 'product',
    'observation_timestamp_utc', 'forecast_max_6h_mm',
    'notified_email', 'notified_telegram', 'note',
]

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger('reconstruct')


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline='') as f:
        return list(csv.DictReader(f))


def _parse_ms(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000
    except Exception:
        return None


def detect_peaks(series: list[tuple[float, float]], thr: float) -> list[dict]:
    """series = [(ms, value)] ordinata. Raggruppa i frame sopra soglia in eventi
    (gap > GAP_MIN separa eventi distinti). Ritorna [{start, end, peak_ms, peak}]."""
    peaks = []
    cur = None
    for ms, v in series:
        if v >= thr:
            if cur is None:
                cur = {'start': ms, 'end': ms, 'peak_ms': ms, 'peak': v, 'last': ms}
            elif ms - cur['last'] > GAP_MIN * 60_000:
                peaks.append(cur)
                cur = {'start': ms, 'end': ms, 'peak_ms': ms, 'peak': v, 'last': ms}
            if v > cur['peak']:
                cur['peak'] = v
                cur['peak_ms'] = ms
            cur['end'] = ms
            cur['last'] = ms
        elif cur is not None and ms - cur['last'] > GAP_MIN * 60_000:
            peaks.append(cur)
            cur = None
    if cur is not None:
        peaks.append(cur)
    return peaks


def area_series(area: str) -> tuple[list[tuple[float, float]], str]:
    """Serie intensità per l'area: ARPA max_mmh (Lombardia) o CUM3 max come proxy.
    Ritorna (series, product)."""
    if area in ARPA_AREAS:
        rows = _read_csv(DATA / f'{area}_arpa.csv')
        s = []
        for r in rows:
            if r.get('location_type') != 'area':
                continue
            ms = _parse_ms(r.get('timestamp_utc', ''))
            try:
                v = float(r.get('max_mmh') or 0)
            except ValueError:
                v = 0.0
            if ms is not None:
                s.append((ms, v))
        if s:
            s.sort()
            return s, 'ARPA'
    # fallback / Panna: CUM3 DPC max
    rows = _read_csv(DATA / f'{area}_cum3.csv')
    s = []
    for r in rows:
        if r.get('product') != 'CUM3' or r.get('location_type') != 'area':
            continue
        ms = _parse_ms(r.get('timestamp_utc', ''))
        try:
            v = float(r.get('max') or 0)
        except ValueError:
            v = 0.0
        if ms is not None:
            s.append((ms, v))
    s.sort()
    return s, 'CUM3'


def known_event_times(events: list[dict]) -> dict[str, list[float]]:
    """Timestamp degli storm_on_area già presenti (reali o ricostruiti), per area."""
    known: dict[str, list[float]] = {}
    for e in events:
        if e.get('level') == 'storm_on_area':
            ms = _parse_ms(e.get('event_timestamp_utc', ''))
            if ms is not None:
                known.setdefault(e['area_name'], []).append(ms)
    return known


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='Non scrive, mostra solo.')
    ap.add_argument('--threshold', type=float, default=RECON_THRESHOLD_MMH)
    args = ap.parse_args()

    events = _read_csv(EVENTS_CSV)
    known = known_event_times(events)

    def near(area: str, ms: float) -> bool:
        return any(abs(t - ms) < DEDUP_H * 3600_000 for t in known.get(area, []))

    new_rows = []
    for area in ALL_AREAS:
        series, product = area_series(area)
        if not series:
            continue
        for pk in detect_peaks(series, args.threshold):
            if near(area, pk['peak_ms']):
                continue
            start_iso = _iso(pk['start'])
            # fine evento: ultimo frame sopra soglia + un piccolo margine
            end_iso = _iso(pk['end'] + 5 * 60_000)
            # apertura
            new_rows.append({
                'event_timestamp_utc': start_iso, 'area_name': area,
                'level': 'storm_on_area', 'threshold_mm': f'{args.threshold:.1f}',
                'observed_mm_mean': '', 'observed_mm_max': f"{pk['peak']:.2f}",
                'product': product, 'observation_timestamp_utc': _iso(pk['peak_ms']),
                'forecast_max_6h_mm': '', 'notified_email': 'recon', 'notified_telegram': 'recon',
                'note': 'ricostruito (evento non registrato dal nowcast)',
            })
            # chiusura (coppia completa: evita che risulti "attivo ora")
            new_rows.append({
                'event_timestamp_utc': end_iso, 'area_name': area,
                'level': 'storm_cleared', 'threshold_mm': f'{args.threshold:.1f}',
                'observed_mm_mean': '', 'observed_mm_max': f"{pk['peak']:.2f}",
                'product': product, 'observation_timestamp_utc': end_iso,
                'forecast_max_6h_mm': '', 'notified_email': 'recon', 'notified_telegram': 'recon',
                'note': 'ricostruito',
            })
            # registro come "noto" per non ri-ricostruire nello stesso run
            known.setdefault(area, []).append(pk['peak_ms'])
            log.info(f"  + evento ricostruito {area} {start_iso} picco {pk['peak']:.1f} mm/h ({product})")

    if not new_rows:
        log.info('Nessun evento da ricostruire: events.csv è già coerente coi dati radar.')
        return 0

    n_events = len(new_rows) // 2
    if args.dry_run:
        log.info(f'[dry-run] ricostruirei {n_events} eventi ({len(new_rows)} righe).')
        return 0

    # Append + riordino cronologico dell'intero file (le pagine si aspettano ordine)
    all_rows = events + new_rows
    all_rows.sort(key=lambda r: r.get('event_timestamp_utc', ''))
    with EVENTS_CSV.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=EVENTS_HEADER)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, '') for k in EVENTS_HEADER})
    log.info(f'✓ scritti {n_events} eventi ricostruiti in events.csv ({len(all_rows)} righe totali).')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log.error(f'FATAL: {e}')
        sys.exit(1)
