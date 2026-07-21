# CLAUDE.md — istruzioni per assistenti che modificano questo repo

Questo file è letto automaticamente da Claude Code all'avvio. Contiene il
contesto di dominio e le regole di lavoro per modificare `radar-dpc` in
sicurezza. Il README.md descrive il "cosa fa" per l'utente; questo file
descrive **come si lavora sul codice senza fare danni**.

---

## ⚠️ Perché questo progetto richiede cautela

Non è un progetto software qualsiasi. Il sistema prende **decisioni operative
reali sulla protezione di sorgenti di acqua minerale** (Nestlé Waters Italia):
in base alle allerte, un operatore decide se mettere offline una sorgente prima
di un evento intenso. Un'allerta mancata o una soglia sbagliata **non è un bug
cosmetico** — è una decisione operativa che non parte.

Conseguenza pratica: **gli errori spesso si vedono solo quando piove.** Non
esiste un test che dica subito "hai rotto le allerte". Quindi ogni modifica alla
logica di allerta va validata con estrema attenzione e mostrata all'utente
prima del commit.

---

## Valori non negoziabili (dell'utente, non del codice)

1. **No invented numbers.** Ogni valore stimato o modellato va **etichettato
   esplicitamente** come tale. I buchi nei dati vanno **dichiarati**, mai
   riempiti con stime plausibili "per far tornare i conti". Se un dato manca,
   si scrive che manca — non lo si inventa.
2. **Dichiara le assunzioni, non seppellirle.** Se una modifica assume qualcosa
   (una soglia, un default, una fonte dati), va detto apertamente, non nascosto
   nel codice.
3. **Delta approach per gli impatti clima.** Si isola la variazione (es. ET
   indotta dal riscaldamento) rispetto al baseline, non si modellano valori
   assoluti.
4. **Trend non significativi vanno smorzati.** Non estrapolare trend a basso R²
   su orizzonti lunghi (gonfia il segnale).
5. **Un solo modello → falsi positivi.** Gli alert forecast richiedono doppia
   conferma (OpenMeteo + MET Norway). **MET Norway non va MAI mediato**
   nell'ensemble: resta validazione indipendente.

---

## Regole di lavoro (come modificare il codice)

- **Valida SEMPRE prima di proporre una modifica.**
  - HTML/JS: estrai i `<script>` e lancia `node --check`; per la logica usa un
    harness jsdom (Node + JSDOM + mock Chart/Leaflet + mock fetch per URL).
  - Python: `python3 -m py_compile <file>` + test mirati della logica.
  - YAML workflow: `python3 -c "import yaml; yaml.safe_load(open('...'))"`.
- **Modifiche chirurgiche e non-breaking.** Preferire edit piccoli e mirati a
  riscritture ampie. Un blocco per volta su modifiche rischiose.
- **Mostra il diff e chiedi conferma prima di committare** qualsiasi cosa
  tocchi la logica di allerta, le soglie, o il calcolo dei mm.
- **Non fare commit automatici** su logica di allerta / soglie / calcolo
  pioggia. Documentazione, test, refactoring cosmetico: ok con revisione.
- Comunicazione con l'utente **in italiano**, concisa, con le assunzioni
  dichiarate.

### Parti DELICATE — non toccare senza revisione esplicita
- `archive/scripts/nowcast.py` — logica "cella su area", trigger ARPA-in-OR,
  milestone cumulata, warning ritardo. Cuore dell'allertamento.
- `archive/scripts/monitor.py` — soglie SRT1/CUM3/VMI e invio allerte.
- `archive/scripts/forecast_matrix.py` / `forecast_ensemble_alert.py` — trigger
  forecast a due canali (soglia + salto), doppia conferma.
- `archive/areas.json` — **unica fonte** di soglie, poligoni, destinatari.
  Cambiare un numero qui cambia il comportamento in produzione.

---

## Architettura in breve

**Frontend** (GitHub Pages): pagine HTML single-file che leggono i dati da
`raw.githubusercontent.com/EbbeneTriglav/radar-dpc/main/archive/data/` via un
helper `fetchData()`. Chart.js + Leaflet.js. Pagine:
`index, storico, archivio, monitor, eventi, arpa, previsioni, verifica`.

**Backend** (GitHub Actions, cron): script Python in `archive/scripts/`.
- `monitor.py` — soglie DPC (SRT1/CUM3/VMI), allerte, scrive `events.csv`.
- `nowcast.py` — "cella su area" (SRI DPC + ARPA in OR), cumulata live, ~20'.
- `arpa_collect.py` — frame ARPA Lombardia (Desio+Flero), PNG live (ultimi 12) +
  archivio eventi in `radar_arpa/events/` per il replay.
- `collect.py` — cumulate CUM3/CUM24 storiche (processa da IERI + `--include-today`).
- `forecast_matrix.py` / `forecast_ensemble_alert.py` — allerte forecast.
- `forecast_verify.py` / `forecast_history.py` — verifica accuratezza.
- `reconstruct_events.py` — ricostruisce in `events.csv` gli eventi persi quando
  Actions si inceppa (coppie storm_on_area + storm_cleared, marcate "ricostruito").
- `healthcheck.py` — sorveglia freshness dei dati; se un workflow è fermo lo
  riavvia via workflow_dispatch (kick).
- `radar_common.py` — modulo condiviso (send_email/telegram, util).

**Proxy** (`cloudflare-worker/worker.js`): proxy CORS per i bucket S3 del DPC +
**watchdog** (`scheduled`, cron Cloudflare ogni 10') che riavvia i workflow
GitHub fermi. È il livello di recovery affidabile: il cron di GitHub è
best-effort e slitta/salta; quello di Cloudflare no. Va deployato a mano su
Cloudflare **e** committato qui (i due devono coincidere).

### Dati chiave (`archive/data/`)
- `events.csv` — **registro storico unico** di eventi e allerte. Letto da 3
  pagine + 5 script. NON è un doppione: è il libro mastro. Colonne:
  `event_timestamp_utc, area_name, level, threshold_mm, observed_mm_mean,
  observed_mm_max, product, observation_timestamp_utc, forecast_max_6h_mm,
  notified_email, notified_telegram, note`.
- `*_cum3.csv` — CUM3 DPC, blocchi 3h a ore fisse (00,03,06...21 UTC),
  **adiacenti e non sovrapposti → sommabili** per la cumulata evento.
- `*_arpa.csv` — ARPA 5-min, `max_mmh`/`mean_mmh`. Copre **solo Ruspino e
  Cepina** (Lombardia), NON Panna.
- `radar_arpa/` — 12 PNG live (rotanti) + `index.json`; `events/<id>/` archivio
  eventi per replay (creato al primo evento post-deploy).

### Aree monitorate
`ruspino` (Bergamo), `cepina` (Levissima, Valtellina), `panna` (Mugello, FI).
Panna è SIR Toscana: **niente ARPA**, il pluviometro (Monte di Fò) è un CSV
giornaliero nel repo `dati_idro` (SIR non ha API CORS).

---

## Fatti tecnici da ricordare (per non re-imparare a ogni sessione)

- **Cumulata evento radar** = somma dei blocchi CUM3 che intersecano la finestra
  evento (non una CUM3 singola: sarebbe finestra fissa 3h, sbagliata per eventi
  brevi o lunghi). Per ARPA = integrale `mm/h × Δt`. Confronto col pluviometro
  è mm↔mm. Il `max` d'area sovrastima (pixel peggiore), il `mean` può diluire:
  il pluviometro puntuale sta tra i due → si mostrano entrambi.
- **CUM3 gap serale/notturno**: `collect.py` deve processare da IERI, non oggi,
  altrimenti i blocchi 21:00/24:00 non esistono ancora → cumulata mancante per
  eventi serali. Già corretto; non regredire.
- **Fetch pluviometro Socrata**: usare filtro temporale `$where` sulla data, non
  `$limit` generico (con dati sub-orari copre solo ~14 giorni → eventi vecchi a 0).
- **Ground sensors**: Cornalita (Ruspino) idsensore ARPA `2278`, Oga
  S.Colombano (Cepina) `8010`, endpoint `dati.lombardia.it/resource/647i-nhxk.json`.
  Dal datacenter il fetch dà 403 (funziona da browser).
- **jsdom**: le `let` top-level non sono su `dom.window`; per leggerle nei test
  esporre con `window.x = x` in coda agli script valutati.
- **Mock Leaflet nei test**: includere `circleMarker`, `bindPopup`, `bindTooltip`,
  `fitBounds`, altrimenti l'init si interrompe e i test falliscono a monte.

---

## Secrets (già configurati, non stamparli mai)
GitHub Actions: `SMTP_HOST/PORT/USER/PASS/TO`, `TELEGRAM_TOKEN`,
`TELEGRAM_CHAT_ID`, `GITHUB_TOKEN` (automatico).
Cloudflare Worker: `GH_TOKEN` (PAT fine-grained, repo radar-dpc, Actions RW),
opzionali `TG_BOT_TOKEN` + `TG_CHAT_ID` per la notifica del watchdog.
**Mai loggare, stampare o committare valori di secret.**

---

## Stato attuale (aggiornare quando cambia)
Sistema in produzione e funzionante. Ultimi lavori: tabella verifica a due radar
(ARPA+DPC, cumulate mm), archivio+replay eventi ARPA, sezione Previsioni,
watchdog Cloudflare + auto-riavvio healthcheck, ricostruzione eventi persi.
Aperti/possibili: allineare granularità MET/OM, pagina Eventi dedicata,
storicizzazione verifica forecast.
