#!/usr/bin/env python3
"""
episodes.py — raggruppa la pioggia in EPISODI (unità fisica, non allerte).

PERCHE'
  La pagina Verifica mostrava una riga per allerta: allerte diverse dello
  stesso temporale (storm, monitor, forecast) diventavano righe separate ma
  con le STESSE cumulate (stessa finestra di pioggia) -> doppioni e rumore,
  specialmente per i temporali notturni a cavallo della mezzanotte.

COSA FA (solo lettura dei dati archiviati, nessuna chiamata di rete)
  1. Costruisce gli intervalli "bagnati" per area:
       - ARPA 5' (Ruspino, Cepina, da giugno 2026): frame bagnato se il
         pixel massimo d'area >= WET_MMH mm/h. Ogni frame copre 5 minuti.
       - DPC CUM3 (tutte le aree): usato SOLO dove ARPA manca (buchi di
         acquisizione, Scarperia, periodo prima di ARPA). Blocco bagnato se il
         massimo d'area >= WET_CUM3_MM mm; il blocco @HH copre [HH-3h, HH].
     Un buco dati NON viene trattato come "asciutto": se ARPA manca, decide CUM3.
  2. Un episodio si chiude solo dopo MIT_H ore asciutte consecutive
     (minimum inter-event time). Così un temporale notturno a cavallo della
     mezzanotte resta UN episodio, e due celle separate da ore restano due.
  3. Per ogni episodio calcola sulla STESSA finestra [inizio, fine]:
       - cumulata ARPA (integrale mm/h x dt) max e media d'area + copertura %
       - cumulata radar DPC SRI (idem, tutte le aree, Scarperia compresa) dai
         CSV di sri_collect.py — radar "puro", confrontabile col pluviometro
       - CUM3 DPC (somma blocchi 3h che intersecano la finestra). ATTENZIONE:
         la CUM3 DPC è ottenuta SOLO dai pluviometri a terra interpolati
         (~3000 stazioni), NON dal radar: è "pioggia osservata", non serve a
         verificare il radar (il suo accordo col pluviometro è in parte circolare).
       - pluviometro a terra (ground_rain.csv) SOLO se la finestra è coperta
         dall'archivio (ground_index.csv); altrimenti vuoto = "non disponibile".
         Scarperia: totale giornaliero SIR Monte di Fò (dati_idro) dei giorni
         toccati dall'episodio, marcato come giornaliero.
       - allerte agganciate (events.csv): conteggi per tipo, prima allerta,
         anticipo rispetto al picco.

OUTPUT
  archive/data/episodes.csv         una riga per episodio (riscritto ogni volta:
                                    è un dato DERIVATO, events.csv resta il
                                    registro storico intatto)
  archive/data/episodes_alerts.csv  legame episodio <-> allerta (per "esplodi")

USO
  python archive/scripts/episodes.py               # scrive i file
  python archive/scripts/episodes.py --calibrate   # sensitività MIT vs pluviometro
Solo stdlib.
"""
import argparse
import csv
import io
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent          # archive/
DATA = BASE / 'data'

# ── Parametri (dichiarati, non nascosti) ─────────────────────────────────────
MIT_H = 3.0          # ore asciutte che separano due episodi (calibrato: vedi --calibrate)
WET_MMH = 0.5        # soglia frame ARPA bagnato (pixel max d'area, mm/h)
WET_CUM3_MM = 1.0    # soglia blocco CUM3 bagnato (max d'area, mm/3h)
MIN_EP_MM = 0.5      # episodi con cumulata ARPA media E DPC media < soglia: scartati (rumore)
GAUGE_PRE_MIN = 15   # margine pluviometro prima dell'inizio (registra a fine intervallo)
GAUGE_POST_MIN = 30  # margine dopo la fine (ritardo radar -> suolo)
ALERT_MARGIN_H = 1.0 # allerte nowcast/monitor agganciate entro ±1h dalla finestra
FORECAST_LEAD_H = 24 # allerte forecast agganciate se emesse fino a 24h prima

ARPA_AREAS = ('ruspino', 'cepina')
# Radar MeteoSwiss (mch_collect.py, dal 22/09/2026): copre Ruspino e Cepina, NON Scarperia.
# Solo DATO DI STUDIO: cumulate per episodio nelle colonne mch_*, non decide né episodi né fonte.
MCH_AREAS = ('ruspino', 'cepina')
# Aree dove il radar ARPA è affidabile (verifica ott-2026: Ruspino r≈0.8 col
# pluviometro; Cepina r≈0.3, fascio probabilmente schermato dai rilievi).
# Dove ARPA NON è affidabile, i suoi frame "asciutti" non possono smentire la
# CUM3 (pluviometri): altrimenti si perdono episodi reali (es. Cepina 21/08).
ARPA_TRUSTED = ('ruspino',)
AREAS = ('ruspino', 'cepina', 'scarperia')
GAUGES = {'ruspino': ('2278', 'Cornalita'), 'cepina': ('8010', 'Oga S.Colombano')}
SIR_URL = ('https://raw.githubusercontent.com/EbbeneTriglav/dati_idro/main/'
           'dati/MonteDiFo_precip_1992-2026.csv')
SIR_NAME = 'Monte di Fò'

STEP = timedelta(minutes=5)
UTC = timezone.utc
ROME = ZoneInfo('Europe/Rome')


def ts(s):
    return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)


def iso(d):
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


# ── Caricamento dati ─────────────────────────────────────────────────────────
def load_arpa(area):
    rows = [r for r in read_csv(DATA / f'{area}_arpa.csv') if r.get('location_type') == 'area']
    out = {}
    for r in rows:
        try:
            out[ts(r['timestamp_utc'])] = (fnum(r['max_mmh']), fnum(r['mean_mmh']))
        except ValueError:
            continue
    return out                       # {t: (max_mmh, mean_mmh)}


def load_sri(area):
    """Radar DPC SRI (5', tutte le aree) archiviato da sri_collect.py:
    raccolta continua + eventuale backfill. Solo frame validi (status ok)."""
    out = {}
    for name in (f'{area}_sri.csv', f'{area}_sri_backfill.csv'):
        for r in read_csv(DATA / name):
            if r.get('status') != 'ok':
                continue
            try:
                out[ts(r['timestamp_utc'])] = (fnum(r['max_mmh']), fnum(r['mean_mmh']))
            except ValueError:
                continue
    return out                       # {t: (max_mmh, mean_mmh)}


def load_mch(area):
    """Radar MeteoSwiss (5') archiviato da mch_collect.py: raccolta continua + backfill.
    Solo frame validi (status ok); 'nodata' = fuori copertura, mai contato come zero."""
    out = {}
    for name in (f'{area}_mch_backfill.csv', f'{area}_mch.csv'):
        for r in read_csv(DATA / name):
            if r.get('status') != 'ok':
                continue
            try:
                out[ts(r['timestamp_utc'])] = (fnum(r['max_mmh']), fnum(r['mean_mmh']))
            except ValueError:
                continue
    return out                       # {t: (max_mmh, mean_mmh)}


def attach_mch(eps, mch):
    """Cumulata MeteoSwiss nella finestra episodio (integrale mm/h x 5'). Vuota se nessun frame."""
    for ep in eps:
        s, e = ep['start'], ep['end']
        fr = [v for t, v in mch.items() if s <= t < e]
        if fr:
            exp = max(1, int((e - s) / STEP))
            ep['mch_max_mm'] = sum(v[0] for v in fr) * 5 / 60
            ep['mch_mean_mm'] = sum(v[1] for v in fr) * 5 / 60
            ep['mch_cov'] = min(100, round(100 * len(fr) / exp))


def load_cum3(area):
    out = {}
    for r in read_csv(DATA / f'{area}_cum3.csv'):
        if r.get('product') != 'CUM3' or r.get('location_type') != 'area':
            continue
        try:
            out[ts(r['timestamp_utc'])] = (fnum(r['max']), fnum(r['mean']))
        except ValueError:
            continue
    return out                       # {t_fine_blocco: (max_mm, mean_mm)}


def load_ground():
    pts = defaultdict(list)
    for r in read_csv(DATA / 'ground_rain.csv'):
        try:
            pts[r['sensor_id']].append((ts(r['ts_utc']), fnum(r['mm'])))
        except ValueError:
            continue
    cover = defaultdict(list)        # area -> [(start, end)] finestre archiviate
    for r in read_csv(DATA / 'ground_index.csv'):
        try:
            cover[r['area_name']].append((ts(r['win_start_utc']), ts(r['win_end_utc'])))
        except ValueError:
            continue
    for a in cover:
        cover[a] = merge_intervals(cover[a], timedelta(0))
    return pts, cover


def load_sir(local=None):
    """Totali giornalieri SIR Monte di Fò. Dal clone locale se indicato
    (env SIR_CSV), altrimenti dal repo dati_idro. Se non raggiungibile -> {}."""
    txt = None
    path = local or os.environ.get('SIR_CSV')
    try:
        if path and Path(path).exists():
            txt = Path(path).read_text(encoding='utf-8')
        else:
            with urllib.request.urlopen(SIR_URL, timeout=30) as r:
                txt = r.read().decode('utf-8')
    except Exception as e:
        print(f'  SIR Monte di Fò non disponibile: {e}', file=sys.stderr)
        return {}
    out = {}
    for line in txt.strip().splitlines()[1:]:
        p = line.split(';')
        if len(p) >= 2:
            try:
                out[p[0]] = float(p[1])
            except ValueError:
                pass
    return out


# ── Intervalli ───────────────────────────────────────────────────────────────
def merge_intervals(iv, gap):
    """Unisce intervalli (s, e) se la distanza tra fine e inizio è < gap."""
    iv = sorted(iv)
    out = []
    for s, e in iv:
        if out and s - out[-1][1] < gap:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def covered(cover, s, e):
    return any(cs <= s and ce >= e for cs, ce in cover)


def wet_intervals(arpa, cum3, wet_mmh, wet_cum3, sri=None, arpa_trusted=True):
    """Intervalli bagnati, per priorità: radar ARPA, poi radar DPC SRI dove ARPA
    manca, poi CUM3 (pluviometri, 3h) per i blocchi senza radar a 5'."""
    sri = sri or {}
    iv = [(t, t + STEP) for t, (mx, _) in arpa.items() if mx >= wet_mmh]
    # SRI: dove ARPA è affidabile serve solo a riempirne i buchi; dove non lo è
    # (Cepina) conta sempre, anche se ARPA ha un frame "asciutto" nello stesso istante
    iv += [(t, t + STEP) for t, (mx, _) in sri.items()
           if (not arpa_trusted or t not in arpa) and mx >= wet_mmh]
    for t_end, (mx, _) in cum3.items():
        if mx < wet_cum3:
            continue
        t0 = t_end - timedelta(hours=3)
        # se il blocco è coperto da radar a 5' (>= 80% dei 36 frame) decide il
        # radar, altrimenti il blocco CUM3 conta bagnato
        n = sum(1 for k in range(36)
                if (arpa_trusted and (t0 + k * STEP) in arpa) or (t0 + k * STEP) in sri)
        if n < 29:
            iv.append((t0, t_end))
    return iv


# ── Episodi ──────────────────────────────────────────────────────────────────
def build_episodes(area, arpa, cum3, mit_h=MIT_H, wet_mmh=WET_MMH,
                   wet_cum3=WET_CUM3_MM, min_mm=MIN_EP_MM, sri=None):
    sri = sri or {}
    iv = merge_intervals(wet_intervals(arpa, cum3, wet_mmh, wet_cum3, sri,
                                       arpa_trusted=area in ARPA_TRUSTED),
                         timedelta(hours=mit_h))
    eps = []
    for s, e in iv:
        ep = {'area_name': area, 'start': s, 'end': e}
        # ARPA nella finestra
        frames = sorted((t, v) for t, v in arpa.items() if s <= t < e)
        exp = max(1, int((e - s) / STEP))
        if frames:
            ep['arpa_max_mm'] = sum(v[0] for _, v in frames) * 5 / 60
            ep['arpa_mean_mm'] = sum(v[1] for _, v in frames) * 5 / 60
            pk = max(frames, key=lambda x: x[1][0])
            ep['peak_mmh'] = pk[1][0]
            ep['peak_time'] = pk[0]
            ep['arpa_cov'] = min(100, round(100 * len(frames) / exp))
        else:
            ep['arpa_cov'] = 0
        # Radar DPC SRI nella finestra (integrale mm/h x 5')
        sfr = sorted((t, v) for t, v in sri.items() if s <= t < e)
        if sfr:
            ep['sri_max_mm'] = sum(v[0] for _, v in sfr) * 5 / 60
            ep['sri_mean_mm'] = sum(v[1] for _, v in sfr) * 5 / 60
            ep['sri_cov'] = min(100, round(100 * len(sfr) / exp))
            pk = max(sfr, key=lambda x: x[1][0])
            ep['sri_peak_mmh'] = pk[1][0]
            if 'peak_time' not in ep or area not in ARPA_TRUSTED:   # senza ARPA (o ARPA non affidabile) il picco lo dà l'SRI
                ep['peak_mmh'], ep['peak_time'], ep['peak_src'] = pk[1][0], pk[0], 'SRI'
        else:
            ep['sri_cov'] = 0
        if 'peak_src' not in ep and 'peak_mmh' in ep:
            ep['peak_src'] = 'ARPA'
        # CUM3 DPC (pluviometri interpolati, NON radar): blocchi che intersecano la finestra
        blocks = [(t, v) for t, v in cum3.items() if t > s and t - timedelta(hours=3) < e]
        ep['cum3_max_mm'] = sum(v[0] for _, v in blocks)
        ep['cum3_mean_mm'] = sum(v[1] for _, v in blocks)
        ep['cum3_blocks'] = len(blocks)
        if 'peak_time' not in ep and blocks:
            b = max(blocks, key=lambda x: x[1][0])
            ep['peak_time'] = b[0] - timedelta(minutes=90)   # centro del blocco (3h)
        parts = [n for n, c in (('ARPA', ep['arpa_cov']), ('SRI', ep['sri_cov'])) if c > 0]
        if max(ep['arpa_cov'], ep['sri_cov']) < 80:
            parts.append('CUM3 3h (pluviometri)')
        ep['source'] = ' + '.join(parts)
        if (ep.get('arpa_mean_mm', 0) < min_mm and ep.get('sri_mean_mm', 0) < min_mm
                and ep['cum3_mean_mm'] < min_mm):
            continue
        eps.append(ep)
    return eps


def attach_gauge(eps, area, gpts, gcover, sir):
    for ep in eps:
        s = ep['start'] - timedelta(minutes=GAUGE_PRE_MIN)
        e = ep['end'] + timedelta(minutes=GAUGE_POST_MIN)
        if area in GAUGES:
            sid, name = GAUGES[area]
            ep['gauge_name'] = name
            if covered(gcover.get(area, []), s, e):
                ep['gauge_mm'] = sum(mm for t, mm in gpts.get(sid, []) if s < t <= e)
                ep['gauge_type'] = '10min'
        elif area == 'scarperia' and sir:
            ep['gauge_name'] = SIR_NAME
            # Il SIR è giornaliero in GIORNO IDROLOGICO: il valore datato D copre
            # dalle 09:00 di D-1 alle 09:00 di D (verificato sui dati: r 0.34 ->
            # 0.80 con SRI, 0.61 -> 0.98 con CUM3). Un istante t appartiene al
            # giorno SIR = data locale di (t + 15h).
            sir_day = lambda t: (t.astimezone(ROME) + timedelta(hours=15)).date()
            d, days = sir_day(ep['start']), []
            while d <= sir_day(ep['end']):
                days.append(d.isoformat()); d += timedelta(days=1)
            ep['_days'] = days
            if all(x in sir for x in days):
                ep['gauge_mm'] = sum(sir[x] for x in days)
                ep['gauge_type'] = f'giornaliero 9→9 ({len(days)} g)'
    # Scarperia: se più episodi toccano lo stesso giorno, il totale giornaliero è
    # CONDIVISO -> non confrontabile col singolo episodio. Lo dichiaro.
    day_eps = defaultdict(int)
    for ep in eps:
        for d in ep.get('_days', []):
            day_eps[d] += 1
    for ep in eps:
        n = max((day_eps[d] for d in ep.get('_days', [])), default=1)
        if n > 1 and 'gauge_mm' in ep:
            ep['gauge_type'] += f', condiviso con altri {n - 1} episodi'


def classify(level):
    if level.startswith('forecast'):
        return 'forecast'
    if level.startswith('nowcast'):
        return 'nowcast'
    if level.startswith('storm'):
        return 'storm'
    return 'monitor'


def attach_alerts(eps, events):
    """Ogni allerta va al più a UN episodio (il primo che la accetta, in ordine
    di tempo). storm_cleared è una chiusura, non un'allerta: non conta."""
    links = []
    by_area = defaultdict(list)
    for ev in events:
        if ev.get('level') == 'storm_cleared':
            continue
        try:
            by_area[ev['area_name']].append((ts(ev['event_timestamp_utc']), ev))
        except (KeyError, ValueError):
            continue
    m = timedelta(hours=ALERT_MARGIN_H)
    for area, evs in by_area.items():
        aeps = sorted((ep for ep in eps if ep['area_name'] == area), key=lambda x: x['start'])
        for t, ev in sorted(evs, key=lambda x: x[0]):
            cls = classify(ev['level'])
            lo = timedelta(hours=FORECAST_LEAD_H) if cls == 'forecast' else m
            for ep in aeps:
                if ep['start'] - lo <= t <= ep['end'] + m:
                    ep.setdefault('alerts', []).append((t, cls, ev))
                    links.append((ep, t, cls, ev))
                    break
    for ep in eps:
        al = ep.get('alerts', [])
        cnt = defaultdict(int)
        for _, cls, _ in al:
            cnt[cls] += 1
        ep['n_alerts'] = len(al)
        for k in ('storm', 'nowcast', 'monitor', 'forecast'):
            ep[f'n_{k}'] = cnt[k]
        now_al = [t for t, cls, _ in al if cls != 'forecast']
        if now_al:
            ep['first_alert'] = min(now_al)
            if ep.get('peak_time'):
                ep['lead_min'] = round((ep['peak_time'] - min(now_al)).total_seconds() / 60)
        fc = [t for t, cls, _ in al if cls == 'forecast']
        if fc and ep.get('peak_time'):
            ep['forecast_lead_h'] = round((ep['peak_time'] - min(fc)).total_seconds() / 3600, 1)
    return links


# ── Scrittura ────────────────────────────────────────────────────────────────
COLS = ['episode_id', 'area_name', 'start_utc', 'end_utc', 'duration_h', 'source',
        'peak_mmh', 'peak_source', 'peak_utc', 'arpa_max_mm', 'arpa_mean_mm', 'arpa_cov_pct',
        'sri_max_mm', 'sri_mean_mm', 'sri_cov_pct', 'sri_peak_mmh',
        'cum3_max_mm', 'cum3_mean_mm', 'cum3_blocks',
        'gauge_name', 'gauge_mm', 'gauge_type',
        'n_alerts', 'n_storm', 'n_nowcast', 'n_monitor', 'n_forecast',
        'first_alert_utc', 'alert_lead_min', 'forecast_lead_h', 'status',
        'mch_max_mm', 'mch_mean_mm', 'mch_cov_pct']


def r1(v):
    return '' if v is None else f'{v:.1f}'


def ep_row(ep, now):
    eid = f"{ep['area_name']}_{ep['start'].strftime('%Y%m%dT%H%M')}"
    ep['id'] = eid
    open_ = now - ep['end'] < timedelta(hours=MIT_H)
    return {
        'episode_id': eid, 'area_name': ep['area_name'],
        'start_utc': iso(ep['start']), 'end_utc': iso(ep['end']),
        'duration_h': r1((ep['end'] - ep['start']).total_seconds() / 3600),
        'source': ep['source'],
        'peak_mmh': r1(ep.get('peak_mmh')),
        'peak_utc': iso(ep['peak_time']) if ep.get('peak_time') else '',
        'arpa_max_mm': r1(ep.get('arpa_max_mm')), 'arpa_mean_mm': r1(ep.get('arpa_mean_mm')),
        'arpa_cov_pct': ep.get('arpa_cov', 0) or '',
        'peak_source': ep.get('peak_src', ''),
        'sri_max_mm': r1(ep.get('sri_max_mm')), 'sri_mean_mm': r1(ep.get('sri_mean_mm')),
        'sri_cov_pct': ep.get('sri_cov', 0) or '', 'sri_peak_mmh': r1(ep.get('sri_peak_mmh')),
        'cum3_max_mm': r1(ep['cum3_max_mm']), 'cum3_mean_mm': r1(ep['cum3_mean_mm']),
        'cum3_blocks': ep['cum3_blocks'],
        'gauge_name': ep.get('gauge_name', ''), 'gauge_mm': r1(ep.get('gauge_mm')),
        'gauge_type': ep.get('gauge_type', ''),
        'n_alerts': ep['n_alerts'], 'n_storm': ep['n_storm'], 'n_nowcast': ep['n_nowcast'],
        'n_monitor': ep['n_monitor'], 'n_forecast': ep['n_forecast'],
        'first_alert_utc': iso(ep['first_alert']) if ep.get('first_alert') else '',
        'alert_lead_min': ep.get('lead_min', ''),
        'forecast_lead_h': ep.get('forecast_lead_h', ''),
        'status': 'in corso' if open_ else 'chiuso',
        'mch_max_mm': r1(ep.get('mch_max_mm')), 'mch_mean_mm': r1(ep.get('mch_mean_mm')),
        'mch_cov_pct': ep.get('mch_cov', '') or '',
    }


def write_csv(path, cols, rows):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def run(sir_local=None, now=None):
    now = now or datetime.now(tz=UTC)
    events = read_csv(DATA / 'events.csv')
    gpts, gcover = load_ground()
    sir = load_sir(sir_local)
    all_eps = []
    for area in AREAS:
        arpa = load_arpa(area) if area in ARPA_AREAS else {}
        cum3 = load_cum3(area)
        sri = load_sri(area)
        eps = build_episodes(area, arpa, cum3, sri=sri)
        attach_gauge(eps, area, gpts, gcover, sir)
        if area in MCH_AREAS:
            attach_mch(eps, load_mch(area))
        all_eps += eps
        print(f'  {area}: {len(eps)} episodi')
    links = attach_alerts(all_eps, events)
    all_eps.sort(key=lambda x: x['start'], reverse=True)
    rows = [ep_row(ep, now) for ep in all_eps]
    write_csv(DATA / 'episodes.csv', COLS, rows)
    lrows = [{'episode_id': ep['id'], 'event_timestamp_utc': ev['event_timestamp_utc'],
              'area_name': ev['area_name'], 'class': cls, 'level': ev['level'],
              'product': ev.get('product', ''), 'observed_mm_max': ev.get('observed_mm_max', ''),
              'note': ev.get('note', '')}
             for ep, t, cls, ev in sorted(links, key=lambda x: x[1], reverse=True)]
    write_csv(DATA / 'episodes_alerts.csv',
              ['episode_id', 'event_timestamp_utc', 'area_name', 'class', 'level',
               'product', 'observed_mm_max', 'note'], lrows)
    n_g = sum(1 for r in rows if r['gauge_mm'] != '')
    print(f'episodes.csv: {len(rows)} episodi ({n_g} con pluviometro), '
          f'{len(lrows)} allerte agganciate su {sum(1 for e in events if e.get("level") != "storm_cleared")}')
    return all_eps


# ── Calibrazione MIT (solo diagnostica, non scrive nulla) ────────────────────
def calibrate(sir_local=None):
    """Per ogni MIT confronta gli episodi radar con gli 'episodi pluviometro'
    (stessa regola MIT applicata ai dati a terra, solo dentro le finestre
    archiviate). Conta: split = un episodio a terra spezzato in più episodi
    radar; merge = un episodio radar che contiene più episodi a terra."""
    gpts, gcover = load_ground()
    print(f'{"area":8} {"MIT":>4} {"episodi":>8} {"confr.":>7} {"split":>6} {"merge":>6} '
          f'{"bias ARPA med":>14} {"bias CUM3 med":>13}')
    for area in ARPA_AREAS:
        arpa, cum3, sri = load_arpa(area), load_cum3(area), load_sri(area)
        sid = GAUGES[area][0]
        g = sorted(gpts.get(sid, []))
        for mit in (1, 2, 3, 4, 6):
            eps = build_episodes(area, arpa, cum3, mit_h=mit, sri=sri)
            # episodi a terra: punti >= 0.2 mm uniti con la stessa regola
            giv = merge_intervals([(t - timedelta(minutes=10), t) for t, mm in g if mm >= 0.2],
                                  timedelta(hours=mit))
            giv = [iv for iv in giv if covered(gcover.get(area, []), iv[0], iv[1])
                   and sum(mm for t, mm in g if iv[0] < t <= iv[1]) >= 1.0]
            split = merge = n = 0
            ba, bd = [], []
            for gs, ge in giv:
                ov = [ep for ep in eps if ep['start'] < ge and ep['end'] > gs]
                if len(ov) > 1:
                    split += 1
            for ep in eps:
                s = ep['start'] - timedelta(minutes=GAUGE_PRE_MIN)
                e = ep['end'] + timedelta(minutes=GAUGE_POST_MIN)
                if not covered(gcover.get(area, []), s, e):
                    continue
                inside = [iv for iv in giv if iv[0] < ep['end'] and iv[1] > ep['start']]
                if len(inside) > 1:
                    merge += 1
                gmm = sum(mm for t, mm in g if s < t <= e)
                if gmm >= 1.0:
                    n += 1
                    ba.append(ep.get('arpa_mean_mm', 0) - gmm)
                    bd.append(ep['cum3_mean_mm'] - gmm)
            med = lambda v: sorted(v)[len(v) // 2] if v else float('nan')
            print(f'{area:8} {mit:>4} {len(eps):>8} {n:>7} {split:>6} {merge:>6} '
                  f'{med(ba):>+14.1f} {med(bd):>+13.1f}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--calibrate', action='store_true', help='sensitività MIT vs pluviometro (non scrive)')
    ap.add_argument('--sir-csv', help='CSV SIR Monte di Fò locale (default: scarica da dati_idro)')
    a = ap.parse_args()
    if a.calibrate:
        calibrate(a.sir_csv)
    else:
        run(a.sir_csv)
