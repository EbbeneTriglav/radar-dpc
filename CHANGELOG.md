# CHANGELOG — Piattaforma Web V2 Radar DPC

## Sessione 2026-05-29 (blocchi 1, 2, 3, 4, 4b + estensioni)

### Blocco 1 — Affidabilità
- 🆕 `archive/scripts/healthcheck.py` — controlla che monitor/nowcast/archive/arpa/forecast
  aggiornino i dati; alert email+Telegram quando un sistema è fermo o rientra (anti-spam via state).
- 🆕 `.github/workflows/healthcheck.yml` — cron `20 */6 * * *` (4 volte/giorno).
- ✏️ `.github/workflows/pages.yml` — cache-busting automatico: ogni deploy riscrive i `?v=...`
  negli HTML con il SHA del commit (no più bump manuale).

### Blocco 2 — Config soglie unica (già in baseline)
- `archive/areas.json` resta l'unica fonte delle soglie per tutti i prodotti
  (osservato SRT1/CUM3, nowcast SRI/SRT1, ensemble 24h).

### Blocco 3 — Modulo Python comune
- 🆕 `archive/scripts/radar_common.py` — utility condivise (HTTP retry, fetch DPC,
  stats poligono, notifiche, persistenza stato). 14 simboli esportati.
- Strategia conservativa: disponibile per nuovi script (es. `arpa_collect.py`);
  gli script esistenti continuano con le loro copie (zero rischio).

### Blocco 4 — Verifica forecast + storicizzazione + pagina Eventi
- 🆕 `archive/scripts/forecast_history.py` — storicizza forecast OpenMeteo+MET+nowcast VMI
  in append-only `forecast_history.jsonl` (idempotente).
- 🆕 `archive/scripts/forecast_verify.py` — confronta forecast vs CUM3 osservato:
  bias, MAE, hit/miss/false-alarm vs soglia warning → `forecast_verification.csv`.
- 🆕 `eventi.html` — pagina Eventi: tabella filtrabile (area/livello/giorni) +
  tile statistiche accuratezza forecast (MAE/bias/hit/miss/FA per source×horizon).
- 🆕 workflow `forecast-history.yml` (orario) e `forecast-verify.yml` (giornaliero).

### Blocco 4b — Mailing list per area + ARPA Lombardia

#### Mailing list per area
- ✏️ `archive/scripts/monitor.py`, `nowcast.py`, `forecast_ensemble_alert.py` —
  `send_email(..., to=...)` e `send_telegram(..., chat_ids=...)` accettano override
  per-area; fallback automatico a `SMTP_TO`/`TELEGRAM_CHAT_ID` env se vuoti.
- ✏️ `archive/areas.json` — ogni area ha `monitoring.recipients.{email,telegram_chat_ids}`.
- ✏️ `monitor.html` — pannello editor inline destinatari (clic su "Modifica soglie"):
  - modifica email + chat ID per ogni area
  - validazione email lato client
  - persistenza in `localStorage`
  - pulsante **"📥 Scarica areas.json"** genera il file da committare nel repo.

#### ARPA Lombardia (Desio + Flero)
- 🆕 `archive/scripts/arpa_collect.py` — scarica GeoTIFF compressi (`.tif.gz`) ogni 5 min
  da `radarlive.arpalombardia.it/CMP`, calcola stats dBZ su Ruspino e Cepina,
  converte in mm/h con Marshall-Palmer (Z=200·R^1.6). Output: `<area>_arpa.csv`.
- 🆕 `.github/workflows/arpa-collect.yml` — cron `*/5 * * * *`.
- 🆕 `arpa.html` — pagina visualizzazione:
  - dashboard Ruspino e Cepina con statistiche (mm/h, dBZ, pixel, campioni)
  - grafico temporale ultime 24h
  - confronto fianco-a-fianco DPC CUM3 vs ARPA aggregato 3h
  - mappa Leaflet con poligoni delle aree + marker dei radar Desio (MB) e Flero (BS)
- ⚠️ **Nessun alert** su dati ARPA in questa fase — solo raccolta parallela
  per check sanity vs DPC. Panna esclusa (fuori copertura).

### Note operative
- **Mailing list**: per attivare destinatari specifici di un'area, vai su Monitor →
  "Modifica soglie" → modifica email/chat ID → "Salva localmente" → "Esporta config"
  → "Scarica areas.json" → committa il file nel repo.
- **ARPA**: alla prima esecuzione del workflow `arpa-collect`, vedrai apparire i dati
  in `arpa.html`. La pagina mostra "in attesa primo fetch" finché il CSV non esiste.
- **Healthcheck**: i secrets SMTP/Telegram sono gli stessi del workflow monitor.
- **Cache busting**: dopo ogni push, GitHub Pages riscrive automaticamente i `?v=...`
  con il SHA del commit, quindi gli utenti vedono sempre l'ultima versione.

### File modificati (riepilogo)
```
.github/workflows/
  arpa-collect.yml         🆕
  forecast-history.yml     🆕
  forecast-verify.yml      🆕
  healthcheck.yml          🆕
  pages.yml                ✏️  cache-busting

archive/scripts/
  arpa_collect.py          🆕
  forecast_history.py      🆕
  forecast_verify.py       🆕
  healthcheck.py           🆕
  radar_common.py          🆕
  monitor.py               ✏️  override email/tg
  nowcast.py               ✏️  override email/tg
  forecast_ensemble_alert.py  ✏️  override email/tg

archive/
  areas.json               ✏️  + monitoring.recipients

root/
  arpa.html                🆕
  eventi.html              🆕
  monitor.html             ✏️  editor recipients + pannello info
  index.html, storico.html, archivio.html  ✏️  nav (link Eventi + ARPA)
```
