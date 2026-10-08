#!/usr/bin/env python3
"""
ground_daily.py — totali giornalieri (UTC) dei pluviometri ARPA a terra.

PERCHE'
  previsioni.html interrogava ARPA Socrata direttamente dal browser. Quando la
  chiamata fallisce (rete aziendale, CORS, Socrata lento) la pagina mostrava
  "n.d." su tutti i giorni passati, anche quelli asciutti. Questo script fa la
  stessa query da GitHub Actions (dove Socrata risponde) e salva i totali nel
  repo: la pagina li legge da raw.githubusercontent.com e usa Socrata live
  solo come integrazione.

OUTPUT
  archive/data/ground_daily.csv
    date_utc,area_name,sensor_id,mm,n_obs,expected_obs,updated_at_utc
  - Ricalcola gli ultimi DAYS giorni (oggi incluso, parziale) a ogni run e
    conserva i giorni piu' vecchi gia' presenti (archivio che cresce).
  - mm = somma delle misure valide del giorno UTC. Le misure mancanti o -999
    NON sono contate come zero: n_obs < expected_obs lo dichiara.
  - expected_obs = misure attese a passo 10' (144 per un giorno intero; per
    oggi solo fino all'ora del run).
  - Se Socrata non risponde per un sensore, le righe esistenti restano com'erano.
"""

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import time  # noqa: E402
from ground_collect import DATA, SENSORS, fetch_socrata, log  # noqa: E402

OUT = DATA / 'ground_daily.csv'
FIELDS = ['date_utc', 'area_name', 'sensor_id', 'mm', 'n_obs', 'expected_obs', 'updated_at_utc']
DAYS = 9            # oggi + 8 giorni passati (la pagina ne mostra 7)
STEP_MIN = 10       # passo misure ARPA


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat().replace('+00:00', 'Z')
    day0 = now.replace(hour=0, minute=0, second=0) - timedelta(days=DAYS - 1)

    rows = {}
    if OUT.exists():
        with OUT.open(newline='', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                rows[(r['date_utc'], r['area_name'])] = r

    n_upd = 0
    for k_s, (area, sensor) in enumerate(SENSORS.items()):
        if k_s:
            time.sleep(3)          # cortesia verso Socrata (limite anonimo)
        try:
            pts = fetch_socrata(sensor['id'], day0, now)
        except Exception as exc:
            log(f'  {area}: Socrata non risponde ({exc}) - righe esistenti invariate')
            continue
        sums, counts = {}, {}
        for ts, mm in pts:
            k = ts.date().isoformat()
            sums[k] = sums.get(k, 0.0) + mm
            counts[k] = counts.get(k, 0) + 1
        for i in range(DAYS):
            d = day0 + timedelta(days=i)
            k = d.date().isoformat()
            end = min(d + timedelta(days=1), now)
            expected = int((end - d).total_seconds() // (STEP_MIN * 60))
            n = counts.get(k, 0)
            rows[(k, area)] = {
                'date_utc': k, 'area_name': area, 'sensor_id': sensor['id'],
                'mm': f'{sums[k]:.1f}' if n else '',
                'n_obs': n, 'expected_obs': expected, 'updated_at_utc': now_iso,
            }
            n_upd += 1
        log(f'  {area} ({sensor["name"]}): {len(pts)} misure, '
            + ', '.join(f'{k[5:]}={sums.get(k, 0):.1f}mm/{counts.get(k, 0)}' for k in sorted(counts)))

    if not n_upd:
        log('Nessun sensore aggiornato.')
        return 0
    with OUT.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for key in sorted(rows):
            w.writerow({f: rows[key].get(f, '') for f in FIELDS})
    log(f'ground_daily.csv: {n_upd} righe aggiornate, {len(rows)} totali.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
