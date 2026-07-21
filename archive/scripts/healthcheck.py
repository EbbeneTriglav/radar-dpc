#!/usr/bin/env python3
"""
healthcheck.py — Controlla che monitor/nowcast/archive aggiornino i dati.

Legge i timestamp da last_observations.json e CSV. Se un sistema non aggiorna
da troppo tempo → alert email+Telegram. Quando rientra → recovery message.
Stato in archive/state/healthcheck_state.json per anti-spam.

Soglie tolleranza (ore):
  monitor  : 1   (cron 15 min)
  nowcast  : 2   (cron 60 min)
  archive  : 14  (cron 6h)
  arpa     : 1   (cron 5 min, opzionale)
  forecast : 26  (cron giornaliero forecast_verify, opzionale)

Eseguito ogni 6h via healthcheck.yml.
"""
from __future__ import annotations
import csv, json, logging, os, smtplib, sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ROOT  = Path(__file__).resolve().parents[1]
DATA  = ROOT / 'data'
STATE = ROOT / 'state' / 'healthcheck_state.json'

MAX_AGE = {  # ore
    'monitor':       2.5,   # dati DPC (la latenza DPC è frequente, tolleranza ampia)
    'monitor_run':   1,     # heartbeat script (se fermo QUI è un problema vero)
    'nowcast':       3,
    'archive':       14,
    'arpa':          2,
    'forecast':      30,
}

# Isteresi: alert solo dopo N check consecutivi oltre soglia (un check ogni 6h)
CONSECUTIVE_FAILS = 2

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
log = logging.getLogger('hc')


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def age_hours(ts: str | None) -> float | None:
    d = parse_iso(ts)
    return None if d is None else (now_utc() - d).total_seconds() / 3600


def send_email(subject: str, body: str) -> str:
    h, port = os.environ.get('SMTP_HOST'), int(os.environ.get('SMTP_PORT', '587'))
    u, pw, to = os.environ.get('SMTP_USER'), os.environ.get('SMTP_PASS'), os.environ.get('SMTP_TO')
    if not (h and u and pw and to):
        return 'skipped'
    try:
        m = MIMEMultipart(); m['Subject'] = subject; m['From'] = u; m['To'] = to
        m.attach(MIMEText(body, 'plain', 'utf-8'))
        with smtplib.SMTP(h, port, timeout=20) as s:
            s.starttls(); s.login(u, pw)
            s.sendmail(u, [x.strip() for x in to.split(',')], m.as_string())
        return 'true'
    except Exception as e:
        log.warning(f'email fail: {e}'); return 'false'


def send_telegram(text: str) -> str:
    tok, chat = os.environ.get('TELEGRAM_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not (tok and chat):
        return 'skipped'
    try:
        r = requests.post(f'https://api.telegram.org/bot{tok}/sendMessage',
                          json={'chat_id': chat, 'text': text, 'parse_mode': 'Markdown'},
                          timeout=20)
        return 'true' if r.ok else 'false'
    except Exception as e:
        log.warning(f'telegram fail: {e}'); return 'false'


def get_monitor_age() -> float | None:
    """Ultimo updated_at_utc dei prodotti in last_observations.json."""
    p = DATA / 'last_observations.json'
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    ts = []
    for k, v in d.items():
        if k.startswith('_') or not isinstance(v, dict):
            continue
        for prod in ('SRT1', 'CUM3'):
            t = (v.get(prod) or {}).get('updated_at_utc')
            if t:
                ts.append(t)
    if not ts:
        return None
    ages = [age_hours(t) for t in ts]
    ages = [a for a in ages if a is not None]
    return min(ages) if ages else None


def get_monitor_run_age() -> float | None:
    """Età heartbeat script monitor (_monitor_last_run_utc)."""
    p = DATA / 'last_observations.json'
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    t = d.get('_monitor_last_run_utc')
    return age_hours(t) if t else None


def get_nowcast_age() -> float | None:
    p = DATA / 'last_observations.json'
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    t = d.get('_nowcast_last_run_utc')
    return age_hours(t) if t else None


def get_csv_max_age(filename: str, column: str = 'fetched_at_utc') -> float | None:
    """Per archive/arpa: ultimo fetched_at_utc nei CSV."""
    f = DATA / filename
    if not f.exists():
        return None
    try:
        with f.open() as fh:
            rdr = csv.DictReader(fh)
            last = None
            for row in rdr:
                v = row.get(column)
                if v: last = v
    except Exception:
        return None
    return age_hours(last) if last else None


def get_archive_age() -> float | None:
    """Età ultimo fetch in panna_cum3.csv (proxy archivio attivo)."""
    return get_csv_max_age('panna_cum3.csv')


def get_arpa_age() -> float | None:
    """Età ultimo fetch ARPA (se il file esiste; opzionale)."""
    for f in ('ruspino_arpa.csv', 'cepina_arpa.csv'):
        a = get_csv_max_age(f)
        if a is not None:
            return a
    return None


def get_forecast_age() -> float | None:
    """Età ultima verifica forecast (opzionale)."""
    return get_csv_max_age('forecast_verification.csv', 'forecast_made_at_utc')


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2, sort_keys=True))


def _fmt_age(h: float) -> str:
    hh = int(h); mm = int(round((h - hh) * 60))
    return f'{hh}h {mm:02d}min'


# Descrizioni leggibili per i messaggi
LABELS = {
    'monitor':     ('Dati radar DPC',         'possibile ritardo lato DPC'),
    'monitor_run': ('Script monitor',          'lo script non gira: controlla Actions'),
    'nowcast':     ('Nowcast celle',           'ritardo cron o script fermo'),
    'archive':     ('Archivio storico',        'workflow archivio fermo'),
    'arpa':        ('Dati radar ARPA',         'workflow arpa-collect fermo o sito ARPA giù'),
    'forecast':    ('Verifica forecast',       'workflow giornaliero in ritardo'),
}

# ─── AUTO-RIAVVIO WORKFLOW FERMI ─────────────────────────────────────────────
# Il cron di GitHub Actions è best-effort e può saltare run. Quando un check
# legato a un workflow va DOWN, oltre a notificare proviamo a riavviarlo via
# workflow_dispatch (serve permissions: actions: write nel workflow + il
# GITHUB_TOKEN standard passato come env). 'monitor' (dati DPC) è escluso:
# lì il problema è lato DPC, non del nostro scheduler.
KICK_WORKFLOWS = {
    'monitor_run': 'monitor.yml',
    'nowcast':     'nowcast.yml',
    'archive':     'archive-daily.yml',
    'arpa':        'arpa-collect.yml',
    'forecast':    'forecast-verify.yml',
}
GH_REPO = os.environ.get('GITHUB_REPOSITORY', 'EbbeneTriglav/radar-dpc')


def kick_workflow(wf: str) -> bool:
    """Riavvia un workflow via workflow_dispatch. Ritorna True se accettato."""
    tok = os.environ.get('GITHUB_TOKEN')
    if not tok:
        log.info(f'  kick {wf}: GITHUB_TOKEN assente, skip')
        return False
    try:
        r = requests.post(
            f'https://api.github.com/repos/{GH_REPO}/actions/workflows/{wf}/dispatches',
            headers={'Authorization': f'Bearer {tok}',
                     'Accept': 'application/vnd.github+json'},
            json={'ref': 'main'}, timeout=20)
        ok = r.status_code == 204
        log.info(f'  🔁 kick {wf}: {"inviato" if ok else f"HTTP {r.status_code}"}')
        return ok
    except Exception as e:
        log.warning(f'  kick {wf} fallito: {e}')
        return False


def check(name: str, age: float | None, max_age: float, state: dict) -> tuple[bool, str]:
    """Isteresi: segnala 'fermo' solo dopo CONSECUTIVE_FAILS check oltre soglia.
    Recovery immediato. Ritorna (changed, status_message)."""
    label, hint = LABELS.get(name, (name, ''))
    prev = state.get(name, {})
    was_down  = prev.get('down', False)
    fail_cnt  = prev.get('fail_count', 0)

    if age is None:
        log.info(f'  {name}: dato non disponibile (opzionale o mai eseguito)')
        return (False, f'{label}: n/d')

    over = age > max_age
    age_s = _fmt_age(age)

    if over:
        fail_cnt += 1
    else:
        fail_cnt = 0

    msg = f'{label}: nessun aggiornamento da {age_s} (atteso entro {max_age}h)'

    # DOWN: solo dopo N check consecutivi falliti
    if over and not was_down and fail_cnt >= CONSECUTIVE_FAILS:
        state[name] = {'down': True, 'fail_count': fail_cnt,
                       'since': now_utc().isoformat().replace('+00:00','Z')}
        log.warning(f'  🔴 {msg}')
        return (True, f'🔴 {msg} — {hint}')
    # RECOVERY: immediato
    if not over and was_down:
        state[name] = {'down': False, 'fail_count': 0,
                       'recovered_at': now_utc().isoformat().replace('+00:00','Z')}
        log.info(f'  🟢 {label}: tornato regolare (ultimo agg. {age_s} fa)')
        return (True, f'🟢 {label}: tornato regolare (ultimo agg. {age_s} fa)')
    # Pending (1° fail) o stato invariato
    state[name] = {'down': was_down, 'fail_count': fail_cnt}
    icon = '🟡' if over else '✓'
    log.info(f'  {icon} {msg if over else f"{label}: OK ({age_s} fa)"}')
    return (False, msg)


def main() -> int:
    state = load_state()

    ages = {
        'monitor':      get_monitor_age(),
        'monitor_run':  get_monitor_run_age(),
        'nowcast':      get_nowcast_age(),
        'archive':  get_archive_age(),
        'arpa':     get_arpa_age(),
        'forecast': get_forecast_age(),
    }
    log.info('=== healthcheck ===')

    changes = []
    statuses = []
    for name, age in ages.items():
        changed, msg = check(name, age, MAX_AGE[name], state)
        # Auto-riavvio: se il check è appena andato DOWN ed è un workflow
        # riavviabile, kick via workflow_dispatch e lo dico nel messaggio.
        if changed and state.get(name, {}).get('down') and name in KICK_WORKFLOWS:
            ok = kick_workflow(KICK_WORKFLOWS[name])
            msg += ' → 🔁 riavvio automatico ' + ('inviato' if ok else 'FALLITO (controlla permessi)')
        statuses.append(msg)
        if changed:
            changes.append(msg)

    if changes:
        subject = f'[radar-dpc] Healthcheck: {len(changes)} cambio/i di stato'
        body = '\n'.join(statuses) + '\n\nDettaglio cambiamenti:\n' + '\n'.join(changes)
        md = '*radar-dpc healthcheck*\n' + '\n'.join(f'• {c}' for c in changes)
        em = send_email(subject, body)
        tg = send_telegram(md)
        log.info(f'notifiche: email={em} tg={tg}')

    save_state(state)
    log.info(f'✓ done — {len(changes)} cambi rilevati')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log.error(f'FATAL: {e}')
        sys.exit(1)
