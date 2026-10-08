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
    Scarperia usa il CSV giornaliero SIR gia' archiviato nel repo dati_idro, che
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
import urllib.error
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
# Il dataset ARPA "realtime" su Socrata si riempie con ritardo (ore, a volte
# un giorno): una finestra archiviata subito dopo la chiusura puo' avere buchi
# (es. 127 misure su 156) e il totale risulta sottostimato. Le finestre con
# meno del 95% delle misure attese vengono ri-scaricate finche' non sono
# passate REFRESH_H ore dalla fine della finestra; le misure gia' archiviate
# restano, si aggiungono solo quelle mancanti.
STEP_MIN = 10
COMPLETE_FRAC = 0.95
REFRESH_H = 72
# Il campo 'data' di Socrata ARPA Lombardia è in ORA SOLARE (UTC+1) tutto
# l'anno, senza fuso nella stringa. Verificato sui dati (ott-2026): radar DPC e
# ARPA si allineano al pluviometro solo spostandolo di -1h. Fino al 05/10/2026
# veniva letto come UTC (pluviometro 1h in ritardo): dati archiviati corretti.
ARPA_SOLAR = timezone(timedelta(hours=1))

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


def _socrata_get(url, tries=5):
    """GET JSON da Socrata con retry. Le chiamate anonime da GitHub Actions
    ricevono spesso HTTP 429 (troppe richieste dallo stesso pool di IP): si
    riprova con attesa crescente (rispettando Retry-After). Con il secret
    opzionale SOCRATA_APP_TOKEN (registrazione gratuita su dati.lombardia.it)
    il limite per IP non si applica. Il token non viene mai stampato."""
    headers = {'User-Agent': 'radar-dpc-ground-collect', 'Accept': 'application/json'}
    tok = (os.environ.get('SOCRATA_APP_TOKEN') or '').strip()
    if tok:
        headers['X-App-Token'] = tok
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            try:
                wait = float(exc.headers.get('Retry-After') or 0)
            except (TypeError, ValueError):
                wait = 0
            wait = min(max(wait, 10 * (i + 1)), 60)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            wait = 10 * (i + 1)
        if i < tries - 1:
            log(f'  Socrata: {type(last).__name__} {getattr(last, "code", "")} - riprovo tra {wait:.0f}s ({i + 1}/{tries - 1})')
            time.sleep(wait)
    raise last


def fetch_socrata(sensor_id, start, end):
    """Misure grezze del sensore nella finestra. Ritorna [(datetime, mm)].
    Solleva eccezione in caso di errore di rete/HTTP: l'evento non viene
    marcato come archiviato e si riprova al giro dopo."""
    # la query va espressa in ora solare, come il campo 'data'
    where = ("data >= '%s' AND data <= '%s'"
             % (start.astimezone(ARPA_SOLAR).strftime('%Y-%m-%dT%H:%M:%S'),
                end.astimezone(ARPA_SOLAR).strftime('%Y-%m-%dT%H:%M:%S')))
    qs = urllib.parse.urlencode({
        'idsensore': sensor_id,
        '$where': where,
        '$order': 'data',
        '$limit': 5000,
    })
    rows = _socrata_get(f'{SOCRATA}?{qs}')
    out = []
    for row in rows:
        raw = (row.get('data') or '')[:19]
        try:
            ts = datetime.fromisoformat(raw).replace(tzinfo=ARPA_SOLAR).astimezone(timezone.utc)
        except ValueError:
            ts = None
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


def expected_points(win_start, win_end):
    return int((win_end - win_start).total_seconds() // (STEP_MIN * 60))


def refresh_incomplete(now, seen, max_refresh=40):
    """Ri-scarica le finestre archiviate con misure mancanti (ritardo ARPA).
    Aggiunge le sole misure nuove a ground_rain.csv e aggiorna la riga di
    ground_index.csv (n_points mai in diminuzione). Ritorna quante finestre
    sono state aggiornate."""
    rows = read_csv(INDEX_FILE)
    todo = []
    for i, r in enumerate(rows):
        s0, e0 = parse_ts(r.get('win_start_utc')), parse_ts(r.get('win_end_utc'))
        col = parse_ts(r.get('collected_at_utc'))
        if not (s0 and e0 and col) or r.get('area_name') not in SENSORS:
            continue
        try:
            n = int(r.get('n_points') or 0)
        except ValueError:
            n = 0
        if n >= COMPLETE_FRAC * expected_points(s0, e0):
            continue
        if col > e0 + timedelta(hours=REFRESH_H):
            continue                     # gia' ricontrollata a ritardo esaurito: buco reale
        todo.append((i, r, s0, e0, n))
    if not todo:
        return 0
    todo = todo[:max_refresh]
    log(f'Finestre incomplete da ricontrollare: {len(todo)}')
    cache, n_upd = {}, 0
    with RAIN_FILE.open('a', newline='', encoding='utf-8') as rain_fh:
        rain_w = csv.DictWriter(rain_fh, fieldnames=RAIN_FIELDS)
        for i, r, s0, e0, n_old in todo:
            sid = SENSORS[r['area_name']]['id']
            ck = (sid, s0, e0)
            try:
                pts = cache[ck] if ck in cache else fetch_socrata(sid, s0, e0)
            except Exception as exc:
                log(f'  ERRORE refresh {r["area_name"]} {r["event_ts_utc"]}: {exc}')
                continue
            cache[ck] = pts
            new = 0
            for pts_ts, mm in pts:
                if mm <= 0:
                    continue
                key = (sid, iso_z(pts_ts))
                if key in seen:
                    continue
                seen.add(key)
                rain_w.writerow({'sensor_id': sid, 'ts_utc': key[1], 'mm': f'{mm:.2f}'})
                new += 1
            total = sum(mm for _, mm in pts)
            if len(pts) >= n_old:
                r['n_points'] = len(pts)
                r['total_mm'] = f'{max(total, float(r.get("total_mm") or 0)):.2f}'
            r['collected_at_utc'] = iso_z(now)
            n_upd += 1
            log(f'  refresh {r["area_name"]} {r["event_ts_utc"]}: {n_old} -> {len(pts)} misure '
                f'(attese {expected_points(s0, e0)}), {new} nuove, totale {total:.1f} mm')
            time.sleep(SLEEP_S)
    with INDEX_FILE.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({f: r.get(f, '') for f in INDEX_FIELDS})
    return n_upd


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
            continue                       # scarperia -> SIR, gestito dalla pagina
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

    # Episodi di pioggia (episodes.py) senza allerte: anche per loro serve il
    # pluviometro, altrimenti il confronto radar/terra resta solo sugli eventi
    # allertati. Archivio una finestra +/-WINDOW_H centrata sull'episodio, solo
    # se l'episodio non e' gia' coperto da finestre archiviate.
    cover = {}
    for r in read_csv(INDEX_FILE):
        s0, e0 = parse_ts(r.get('win_start_utc')), parse_ts(r.get('win_end_utc'))
        if s0 and e0:
            cover.setdefault(r['area_name'], []).append((s0, e0))
    for ep in read_csv(DATA / 'episodes.csv'):
        area = ep.get('area_name', '')
        s0, e0 = parse_ts(ep.get('start_utc')), parse_ts(ep.get('end_utc'))
        if area not in SENSORS or not s0 or not e0 or ep.get('status') != 'chiuso':
            continue
        if any(cs <= s0 - timedelta(minutes=15) and ce >= e0 + timedelta(minutes=30)
               for cs, ce in cover.get(area, [])):
            continue
        mid = s0 + (e0 - s0) / 2
        if (e0 - s0) > timedelta(hours=2 * WINDOW_H - 2):
            log(f'  skip episodio {area} {iso_z(s0)}: piu\' lungo della finestra')
            continue
        ts_raw = iso_z(mid.replace(microsecond=0))
        key = (area, ts_raw)
        if key in done or key in keys_seen or mid + timedelta(hours=WINDOW_H) > now:
            continue
        keys_seen.add(key)
        todo.append((area, ts_raw, mid))

    n_ref = refresh_incomplete(now, seen)

    if not todo:
        log('Nessun evento nuovo da archiviare.' + (f' ({n_ref} finestre completate)' if n_ref else ''))
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
