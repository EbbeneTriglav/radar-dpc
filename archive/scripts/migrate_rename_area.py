#!/usr/bin/env python3
"""
migrate_rename_area.py — rinomina un'area nei file dati e di stato del repo.

    python3 archive/scripts/migrate_rename_area.py OLD NEW [--dry-run]

Cosa fa (solo dentro archive/data e archive/state):
  1. file che si chiamano OLD_*.csv / OLD.xlsx / basemaps/OLD.* → NEW_* / NEW.* ;
     se il file NEW esiste gia' (un'Action ha scritto col nome nuovo nel frattempo)
     le righe vengono UNITE senza duplicati, ordinate per la prima colonna (timestamp);
  2. dentro tutti i file di testo (csv, jsonl, json) sostituisce il token OLD
     (anche come prefisso: OLD_v1, OLD_20260101T0000) con NEW;
  3. i .json vengono ricaricati e riscritti (eventuali chiavi doppie dopo la
     sostituzione → resta l'ultima).
Idempotente: rilanciato dopo la migrazione non cambia nulla. Si puo' rilanciare
piu' tardi per assorbire righe col vecchio nome scritte da run gia' in volo.
Nessuna dipendenza esterna (l'xlsx viene rimosso e rigenerato dal prossimo run
di collect.py, che lo ricostruisce dai CSV).
"""

import argparse
import json
import re
import sys
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parents[1]
ROOTS = [ARCHIVE / 'data', ARCHIVE / 'state']
TEXT_EXT = {'.csv', '.jsonl', '.json', '.txt'}


def token_re(old):
    return re.compile(r'(?<![A-Za-z0-9])' + re.escape(old) + r'(?![a-z])')


def merge_csv(src_text, dst_text):
    s_lines = src_text.splitlines()
    d_lines = dst_text.splitlines()
    if not d_lines:
        return src_text
    header, body = d_lines[0], d_lines[1:]
    seen = set(body)
    extra = [l for l in s_lines[1:] if l and l not in seen]
    rows = sorted(body + extra, key=lambda l: l.split(',', 1)[0])
    return '\n'.join([header] + rows) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('old')
    ap.add_argument('new')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    rx = token_re(a.old)
    n_ren = n_mrg = n_txt = n_del = 0

    # 1) rinomina / unisci file
    for root in ROOTS:
        for p in sorted(root.rglob('*')):
            if not p.is_file() or not rx.match(p.name):
                continue
            q = p.with_name(rx.sub(a.new, p.name, count=1))
            if p.suffix == '.xlsx':                         # derivato: rigenerato da collect.py
                print(f'  rimuovo {p.relative_to(ARCHIVE)} (rigenerato come {q.name})')
                if not a.dry_run:
                    p.unlink()
                n_del += 1
                continue
            if q.exists():
                if p.suffix in ('.csv', '.jsonl'):
                    src = rx.sub(a.new, p.read_text(encoding='utf-8'))
                    if p.suffix == '.csv':
                        out = merge_csv(src, q.read_text(encoding='utf-8'))
                    else:
                        have = q.read_text(encoding='utf-8').splitlines()
                        seen = set(have)
                        out = '\n'.join(have + [l for l in src.splitlines() if l and l not in seen]) + '\n'
                    print(f'  unisco {p.relative_to(ARCHIVE)} → {q.name}')
                    if not a.dry_run:
                        q.write_text(out, encoding='utf-8')
                        p.unlink()
                    n_mrg += 1
                else:                                       # basemap ecc.: tengo il nuovo
                    print(f'  rimuovo {p.relative_to(ARCHIVE)} ({q.name} esiste gia)')
                    if not a.dry_run:
                        p.unlink()
                    n_del += 1
            else:
                print(f'  rinomino {p.relative_to(ARCHIVE)} → {q.name}')
                if not a.dry_run:
                    p.rename(q)
                n_ren += 1

    # 2) sostituzione nel contenuto
    for root in ROOTS:
        for p in sorted(root.rglob('*')):
            if not p.is_file() or p.suffix not in TEXT_EXT:
                continue
            try:
                t = p.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            if not rx.search(t):
                continue
            t2 = rx.sub(a.new, t)
            if p.suffix == '.json':
                try:
                    obj = json.loads(t2)
                    t2 = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
                except Exception:
                    pass
            print(f'  aggiorno contenuto {p.relative_to(ARCHIVE)} ({len(rx.findall(t))} occorrenze)')
            if not a.dry_run:
                p.write_text(t2, encoding='utf-8')
            n_txt += 1

    print(f'Fatto: {n_ren} rinominati, {n_mrg} uniti, {n_del} rimossi, {n_txt} aggiornati'
          + (' (DRY-RUN, nulla scritto)' if a.dry_run else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
