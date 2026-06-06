#!/usr/bin/env python3
"""
forecast_history.py — Storicizza forecast e nowcast in append-only JSONL.

Letto da `last_observations.json` (snapshot vivo), produce
`archive/data/forecast_history.jsonl`: una riga per ogni (area, run) con
forecast OpenMeteo + MET Norway + nowcast VMI.

Da schedulare ogni ora (o ad ogni run monitor). Append-only e idempotente:
salta se l'ultimo record per (area, updated_at) è già presente.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
LAST_OBS = DATA / 'last_observations.json'
HISTORY = DATA / 'forecast_history.jsonl'

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger('hist')


def last_history_keys(file: Path, max_lines: int = 200) -> set[tuple[str, str]]:
    """Ritorna le ultime N coppie (area, updated_at) per evitare duplicati."""
    if not file.exists():
        return set()
    seen = set()
    with file.open() as fh:
        lines = fh.readlines()[-max_lines:]
    for line in lines:
        try:
            r = json.loads(line)
            seen.add((r.get('area_name', ''), r.get('updated_at_utc', '')))
        except Exception:
            continue
    return seen


def main() -> int:
    if not LAST_OBS.exists():
        log.info(f'{LAST_OBS} mancante, skip')
        return 0
    try:
        data = json.loads(LAST_OBS.read_text())
    except Exception as e:
        log.error(f'{LAST_OBS} parse fallito: {e}')
        return 1

    seen = last_history_keys(HISTORY)
    n_new = 0
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open('a') as fh:
        for area_name, area_data in data.items():
            if area_name.startswith('_') or not isinstance(area_data, dict):
                continue
            fc = area_data.get('forecast') or {}
            vmi = area_data.get('VMI_nowcast') or {}
            updated = fc.get('updated_at_utc') or vmi.get('updated_at_utc')
            if not updated:
                continue
            if (area_name, updated) in seen:
                continue
            record = {
                'area_name': area_name,
                'updated_at_utc': updated,
                'openmeteo': fc.get('openmeteo') or {},
                'metno': fc.get('metno') or {},
                'horizon_hours': fc.get('horizon_hours'),
                'vmi_nowcast': {
                    'max_dbz': vmi.get('max_dbz'),
                    'pct_strong': vmi.get('pct_strong'),
                    'buffer_km': vmi.get('buffer_km'),
                    'timestamp_utc': vmi.get('timestamp_utc'),
                } if vmi else None,
            }
            fh.write(json.dumps(record, sort_keys=True) + '\n')
            n_new += 1

    log.info(f'✓ {n_new} nuovi record in {HISTORY.name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
