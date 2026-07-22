#!/usr/bin/env python3
"""
ground_collect.py — archivia i dati dei pluviometri a terra per ogni evento.

PERCHE'
  verifica.html leggeva il pluviometro live da ARPA Socrata a ogni apertura
  pagina: una chiamata per evento, in sequenza. Per non bloccare la pagina il
  riempimento era limitato alle prime 20 righe -> gli eventi piu' vecchi
  restavano senza dato. Inoltre il dataset realtime di Socrata non garantisce
  ritenzione illimitata: quando ARPA cancella, il dato e' perso per sempre.

COSA FA
  Per ogni evento in events.csv non ancora archiviato, scarica dal sensore
  ARPA di riferimento le misure grezze (passo 10') nella finestra
  evento +/- 13h e le appende a ground_rain.csv. La finestra +/-13h copre con
  margine la finestra dinamica calcolata da verifica.html (eventWindow(),
  limitata a +/-12h +30' di margine), cosi' la pagina puo' continuare a usare
  la SUA logica di finestra sui dati archiviati.

OUTPUT (entrambi append-only, mai riscritti)
  archive/data/ground_rain.csv   sensor_id,ts_utc,mm
                                 solo punti con mm > 0 (gli zeri non cambiano
                                 le somme e triplicherebbero il file);
                                 deduplicati su (sensor_id, ts_utc).
  archive/data/ground_index.csv  event_ts_utc,area_name,sensor_id,
                                 win_start_utc,win_end_utc,n_points,total_mm,
                                 collected_at_utc
                                 elenco eventi gia' archiviati: serve allo
                                 script per non rifare il lavoro e alla pagina
                                 per sapere quali eventi ha in locale.

NOTE
  - Solo sensori ARPA Lombardia (Cornalita->ruspino, Oga S.Colombano->cepina).
    Panna usa il CSV giornaliero SIR gia' archiviato nel repo dati_idro, che
    non ha problemi di ritenzione: resta gestito live dalla pagina.
  - Valori negativi (-999 = dato mancante in ARPA) scartati, non azzerati.
  - Un evento viene archiviato solo se la finestra e' interamente nel passato
    (fine < adesso), altrimenti si aspetta il giorno dopo: niente eventi
    troncati a meta'.
  - Solo stdlib: nessuna dipendenza da installare.
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # archive/
DATA = BASE / 'data'
EVENTS_FILE = DATA / 'events.csv'
RAIN_FILE = DATA / 'ground_rain.csv'
INDEX_FILE = DATA / 'ground_index.csv'

SOCRATA = 'https://www.dati.lombardia.it/resource/647i-nhxk.json'

# area -> sensore pluviometrico ARPA di riferimento
SENSORS = {
    'ruspino': {'id': '2278', 'name': 'Cornalita'},
    'cepina':  {'id': '8010', 'name': 'Oga S.Colombano'},
}

WINDOW_H = 13            # semi-ampiezza finestra archiviata (ore)
MAX_EVENTS_PER_RUN = int(os.environ.get('GROUND_MAX_EVENTS', '250'))
SLEEP_S = 0.4            # pausa tra chiamate Socrata (cortesia verso l'API)
HTTP_TIMEOUT = 60

RAIN_FIELDS = ['sensor_id', 'ts_utc', 'mm']
INDEX_FIELDS = ['event_ts_utc', 'area_name', 'sensor_id', 'win_start_utc',
                'win_end_utc', 'n_points', 'total_mm', 'collected_at_utc']


def log(msg):
    print(msg, flush=True)


def parse_ts(s):
    if not s:
        return None
    s = s.strip().replace('Z', '+00:00')
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso_z(d):
    return d.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def ensure_header(path, fields):
    if not path.exists() or path.stat().st_size == 0:
        with path.open('w', newline='', encoding='utf-8') as fh:
            csv.DictWriter(fh, fieldnames=fields).writeheader()


def fetch_socrata(sensor_id, start, end):
    """Misure grezze del sensore nella finestra. Ritorna [(datetime, mm)].
    Solleva eccezione in caso di errore di rete/HTTP: l'evento non viene
    marcato come archiviato e si riprova al giro dopo."""
    where = ("data >= '%s' AND data <= '%s'"
             % (start.strftime('%Y-%m-%dT%H:%M:%S'),
                end.strftime('%Y-%m-%dT%H:%M:%S')))
    qs = urllib.parse.urlencode({
        'idsensore': sensor_id,
        '$where': where,
        '$order': 'data',
        '$limit': 5000,
    })
    req = urllib.request.Request(
        f'{SOCRATA}?{qs}',
        headers={'User-Agent': 'radar-dpc-ground-collect', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        rows = json.load(resp)
    out = []
    for row in rows:
        ts = parse_ts(row.get('data'))
        try:
            mm = float(row.get('valore'))
        except (TypeError, ValueError):
            continue
        # -999 = dato mancante ARPA: scartato, non convertito in zero
        if ts is None or mm < 0:
            continue
        out.append((ts, mm))
    out.sort(key=lambda p: p[0])
    return out


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    ensure_header(RAIN_FILE, RAIN_FIELDS)
    ensure_header(INDEX_FILE, INDEX_FIELDS)

    events = read_csv(EVENTS_FILE)
    if not events:
        log('events.csv vuoto o assente: niente da fare.')
        return 0

    # Eventi gia' archiviati: chiave (area, timestamp evento)
    done = {(r['area_name'], r['event_ts_utc']) for r in read_csv(INDEX_FILE)}
    # Punti gia' presenti: chiave (sensore, timestamp misura)
    seen = {(r['sensor_id'], r['ts_utc']) for r in read_csv(RAIN_FILE)}
    log(f'Archivio attuale: {len(done)} eventi, {len(seen)} misure.')

    now = datetime.now(timezone.utc)

    # Deduplico gli eventi per (area, timestamp): due allerte con lo stesso
    # istante sulla stessa area condividono la stessa finestra pluviometrica.
    todo, keys_seen = [], set()
    for ev in events:
        area = (ev.get('area_name') or '').strip()
        ts_raw = (ev.get('event_timestamp_utc') or '').strip()
        if area not in SENSORS:
            continue                       # panna -> SIR, gestito dalla pagina
        ts = parse_ts(ts_raw)
        if ts is None:
            continue
        key = (area, ts_raw)
        if key in done or key in keys_seen:
            continue
        if ts + timedelta(hours=WINDOW_H) > now:
            log(f'  skip {area} {ts_raw}: finestra non ancora chiusa')
            continue
        keys_seen.add(key)
        todo.append((area, ts_raw, ts))

    if not todo:
        log('Nessun evento nuovo da archiviare.')
        return 0

    todo.sort(key=lambda t: t[2], reverse=True)     # prima i piu' recenti
    if len(todo) > MAX_EVENTS_PER_RUN:
        log(f'{len(todo)} eventi da archiviare, limite {MAX_EVENTS_PER_RUN} '
            f'per run: il resto al giro successivo.')
        todo = todo[:MAX_EVENTS_PER_RUN]

    log(f'Da archiviare: {len(todo)} eventi.')

    n_ok = n_err = n_new_pts = 0
    rain_fh = RAIN_FILE.open('a', newline='', encoding='utf-8')
    index_fh = INDEX_FILE.open('a', newline='', encoding='utf-8')
    rain_w = csv.DictWriter(rain_fh, fieldnames=RAIN_FIELDS)
    index_w = csv.DictWriter(index_fh, fieldnames=INDEX_FIELDS)

    try:
        for area, ts_raw, ts in todo:
            sensor = SENSORS[area]
            sid = sensor['id']
            win_start = ts - timedelta(hours=WINDOW_H)
            win_end = ts + timedelta(hours=WINDOW_H)
            try:
                pts = fetch_socrata(sid, win_start, win_end)
            except Exception as exc:
                n_err += 1
                log(f'  ERRORE {area} {ts_raw}: {exc}')
                time.sleep(SLEEP_S)
                continue

            total = 0.0
            written = 0
            for pts_ts, mm in pts:
                total += mm
                if mm <= 0:
                    continue                       # zeri non archiviati
                key = (sid, iso_z(pts_ts))
                if key in seen:
                    continue
                seen.add(key)
                rain_w.writerow({'sensor_id': sid, 'ts_utc': key[1],
                                 'mm': f'{mm:.2f}'})
                written += 1

            index_w.writerow({
                'event_ts_utc': ts_raw,
                'area_name': area,
                'sensor_id': sid,
                'win_start_utc': iso_z(win_start),
                'win_end_utc': iso_z(win_end),
                'n_points': len(pts),
                'total_mm': f'{total:.2f}',
                'collected_at_utc': iso_z(datetime.now(timezone.utc)),
            })
            rain_fh.flush()
            index_fh.flush()
            n_ok += 1
            n_new_pts += written
            log(f'  {area} {ts_raw}: {len(pts)} misure, {total:.1f} mm '
                f'({written} nuove)')
            time.sleep(SLEEP_S)
    finally:
        rain_fh.close()
        index_fh.close()

    log(f'Fatto: {n_ok} eventi archiviati, {n_new_pts} misure nuove, '
        f'{n_err} errori.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
