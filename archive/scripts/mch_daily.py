#!/usr/bin/env python3
"""
mch_daily.py — totali giornalieri (giorno UTC) del radar MeteoSwiss su Ruspino e Cepina.

Legge <area>_mch.csv + <area>_mch_backfill.csv (frame 5' di mch_collect.py, media e max
sull'area in mm/h) e scrive archive/data/mch_daily.csv (file DERIVATO, riscritto ogni volta):
    date_utc,area_name,mm_mean,mm_max,n_frames,expected_frames,updated_at_utc
  - mm = integrale mm/h x 5' dei soli frame validi (status ok). Frame mancanti o fuori
    copertura NON sono contati come zero: n_frames < expected_frames lo dichiara.
  - expected_frames = 288 per un giorno intero; per oggi solo fino all'ora del run.
Usato da previsioni.html (osservato "Radar MeteoSwiss"). Scarperia è fuori copertura.
Dato di STUDIO (CC BY 4.0, "Fonte: MeteoSwiss"): non vota nelle allerte.
"""

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
OUT = DATA / 'mch_daily.csv'
AREAS = ('ruspino', 'cepina')
FIELDS = ['date_utc', 'area_name', 'mm_mean', 'mm_max', 'n_frames', 'expected_frames', 'updated_at_utc']


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = []
    for area in AREAS:
        frames = {}
        for name in (f'{area}_mch_backfill.csv', f'{area}_mch.csv'):
            p = DATA / name
            if not p.exists():
                continue
            with p.open(newline='', encoding='utf-8') as fh:
                for r in csv.DictReader(fh):
                    if r.get('status') != 'ok':
                        continue
                    try:
                        frames[r['timestamp_utc']] = (float(r['mean_mmh']), float(r['max_mmh']))
                    except (KeyError, ValueError):
                        continue
        days = {}
        for t, (mean, mx) in frames.items():
            d = days.setdefault(t[:10], [0.0, 0.0, 0])
            d[0] += mean * 5 / 60
            d[1] += mx * 5 / 60
            d[2] += 1
        for k in sorted(days):
            d0 = datetime.fromisoformat(k).replace(tzinfo=timezone.utc)
            end = min(d0 + timedelta(days=1), now)
            expected = min(288, int((end - d0).total_seconds() // 300) + (1 if end < d0 + timedelta(days=1) else 0))
            mm_mean, mm_max, n = days[k]
            rows.append({'date_utc': k, 'area_name': area, 'mm_mean': f'{mm_mean:.1f}',
                         'mm_max': f'{mm_max:.1f}', 'n_frames': n, 'expected_frames': expected,
                         'updated_at_utc': now.isoformat().replace('+00:00', 'Z')})
        print(f'  {area}: {len(frames)} frame validi, {len(days)} giorni')
    with OUT.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator='\n')
        w.writeheader()
        w.writerows(rows)
    print(f'mch_daily.csv: {len(rows)} righe')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
