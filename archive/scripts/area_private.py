#!/usr/bin/env python3
"""
area_private.py — dati PRIVATI delle aree (destinatari e nomi usati nei messaggi).

Il repo è pubblico: in areas.json restano solo geometrie e soglie. Destinatari email,
chat Telegram ed eventuale nome "interno" dell'area stanno nel secret GitHub
`AREAS_PRIVATE` (JSON), passato come variabile d'ambiente ai workflow che inviano allerte:

    {
      "ruspino":   {"label": "Ruspino",   "recipients": {"email": [...], "telegram_chat_ids": [...]}},
      "cepina":    {"label": "Cepina",    "recipients": {...}},
      "scarperia": {"label": "Nome interno", "recipients": {...}}
    }

- `label` = nome usato SOLO in email e Telegram (privati). Pagine, log e file dati usano il
  `label` pubblico di areas.json.
- Se il secret manca, si usano i destinatari di default (SMTP_TO, TELEGRAM_CHAT_ID) e il
  nome pubblico: le allerte partono comunque, ma solo ai default (lo dice il log).
Solo libreria standard: importabile da qualunque script.
"""

import json
import logging
import os

log = logging.getLogger('area_private')
_CACHE = None


def private_config():
    global _CACHE
    if _CACHE is None:
        raw = (os.environ.get('AREAS_PRIVATE') or '').strip()
        _CACHE = {}
        if raw:
            try:
                _CACHE = json.loads(raw)
            except Exception as e:                      # mai stampare il contenuto
                log.warning(f'AREAS_PRIVATE non leggibile ({type(e).__name__}): uso i destinatari di default')
        else:
            log.warning('AREAS_PRIVATE non impostato: destinatari di default (SMTP_TO / TELEGRAM_CHAT_ID)')
    return _CACHE


def apply_private(areas):
    """Aggiunge a ogni area i destinatari privati e `label_msg` (nome per i messaggi)."""
    priv = private_config()
    for a in areas:
        p = priv.get(a.get('name'), {}) or {}
        if p.get('recipients'):
            a.setdefault('monitoring', {})['recipients'] = p['recipients']
        a['label_msg'] = p.get('label') or a.get('label') or a.get('name')
    return areas


def recipients(area_name):
    """(email_list|None, telegram_list|None) dal secret; None → default dell'ambiente."""
    r = (private_config().get(area_name, {}) or {}).get('recipients') or {}
    return (r.get('email') or None, r.get('telegram_chat_ids') or None)


def msg_label(area_or_name, default=None):
    """Nome dell'area per email/Telegram: privato se configurato, altrimenti pubblico."""
    if isinstance(area_or_name, dict):
        return area_or_name.get('label_msg') or area_or_name.get('label') or area_or_name.get('name')
    p = private_config().get(area_or_name, {}) or {}
    return p.get('label') or default or area_or_name
