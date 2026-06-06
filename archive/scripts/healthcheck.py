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
    'monitor':  1,
    'nowcast':  2,
    'archive':  14,
    'arpa':     1,
    'forecast': 26,
}

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


def check(name: str, age: float | None, max_age: float, state: dict) -> tuple[bool, str]:
    """Ritorna (changed, status_message). Aggiorna state in place."""
    prev = state.get(name, {'down': False})
    was_down = prev.get('down', False)
    if age is None:
        # Subsystem opzionale (arpa/forecast) o mai eseguito → no alert
        log.info(f'  {name}: dato non disponibile (opzionale o mai eseguito)')
        return (False, f'{name}: n/a')
    is_down = age > max_age
    msg = f'{name}: età {age:.1f}h (limite {max_age}h)'
    if is_down and not was_down:
        state[name] = {'down': True, 'since': now_utc().isoformat().replace('+00:00','Z'),
                       'age_h': round(age, 2)}
        log.warning(f'  🔴 {msg} → ALERT')
        return (True, f'🔴 {msg} → fermo')
    if not is_down and was_down:
        state[name] = {'down': False, 'recovered_at': now_utc().isoformat().replace('+00:00','Z')}
        log.info(f'  🟢 {msg} → RECOVERY')
        return (True, f'🟢 {msg} → ripreso')
    state[name] = {'down': is_down, 'last_check_age_h': round(age, 2)}
    log.info(f'  {("🟡" if is_down else "✓")} {msg}')
    return (False, msg)


def main() -> int:
    state = load_state()

    ages = {
        'monitor':  get_monitor_age(),
        'nowcast':  get_nowcast_age(),
        'archive':  get_archive_age(),
        'arpa':     get_arpa_age(),
        'forecast': get_forecast_age(),
    }
    log.info('=== healthcheck ===')

    changes = []
    statuses = []
    for name, age in ages.items():
        changed, msg = check(name, age, MAX_AGE[name], state)
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
