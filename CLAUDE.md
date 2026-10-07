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
   conferma: OpenMeteo + (MET Norway **oppure** MeteoSwiss ICON-CH1, dal 10/2026).
   **MET Norway e ICON-CH1 non vanno MAI mediati** nell'ensemble: restano
   validazioni indipendenti.

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
`index, storico, archivio, monitor, eventi, arpa, mch, previsioni, verifica, report`.
`mch.html` = radar MeteoSwiss live (PNG da `archive/data/radar_mch/`, dato di studio). Tema: chiave localStorage
`radar-theme` + `body.light-theme` su tutte le pagine.
`report.html` genera nel browser report deterministici (sintesi stakeholder + tecnico)
da `episodes.csv`, `episodes_alerts.csv`, `events.csv`, `forecast_verification.csv`.

**Backend** (GitHub Actions, cron): script Python in `archive/scripts/`.
- `monitor.py` — soglie DPC (SRT1/CUM3/VMI), allerte, scrive `events.csv`.
- `nowcast.py` — "cella su area" (SRI DPC + ARPA secondo `ARPA_ROLE`), cumulata live, ogni 10'.
- `arpa_collect.py` — frame ARPA Lombardia (Desio+Flero), PNG live (ultimi 12) +
  archivio eventi in `radar_arpa/events/` per il replay.
- `collect.py` — cumulate CUM3/CUM24 storiche (processa da IERI + `--include-today`).
- `forecast_matrix.py` / `forecast_ensemble_alert.py` — allerte forecast.
- `forecast_verify.py` / `forecast_history.py` — verifica accuratezza.
- `sri_collect.py` — archivio radar DPC SRI (5', tutte le aree) in `<area>_sri.csv`;
  `--probe` = profondità storica API, `--backfill-episodes` = recupero finestre episodio.
  Scrive anche l'anello 10 km (`<area>_sri_ring*.csv`, dati di studio pre-allerta, `--backfill-ring`).
- `episodes.py` — episodi di pioggia (chiusura dopo 3h asciutte, MIT calibrato col
  pluviometro): `episodes.csv` è DERIVATO e riscritto, `events.csv` resta il libro mastro.
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
- `radar_mch/` — 12 PNG MeteoSwiss (Web Mercator, rotanti) + `index.json`, scritti da `mch_collect.py`.
- `radar_arpa/` — 12 PNG live (rotanti) + `index.json`; `events/<id>/` archivio
  eventi per replay (creato al primo evento post-deploy).

### Aree monitorate
`ruspino` (Bergamo), `cepina` (Levissima, Valtellina), `panna` (Mugello, FI).
Panna è SIR Toscana: **niente ARPA**, il pluviometro (Monte di Fò) è un CSV
giornaliero nel repo `dati_idro` (SIR non ha API CORS).

---

## Fatti tecnici da ricordare (per non re-imparare a ogni sessione)

- **Cumulata evento**: radar (SRI DPC, ARPA) = integrale `mm/h × Δt` sulla finestra
  episodio; CUM3 (pluviometri) = somma dei blocchi 3h che intersecano la finestra
  (non una CUM3 singola: finestra fissa 3h, sbagliata per eventi brevi o lunghi). Confronto col pluviometro
  è mm↔mm. Il `max` d'area sovrastima (pixel peggiore), il `mean` può diluire:
  il pluviometro puntuale sta tra i due → si mostrano entrambi.
- **CUM3/CUM6/CUM12/CUM24 DPC NON sono radar**: sono pluviometri a terra interpolati
  (~3000 stazioni, doc DPC). SRT1 = radar SRI integrato con i pluviometri. Radar "puro" =
  SRI DPC (5', tutte le aree) e ARPA (5', solo Lombardia). Nei report e nelle verifiche
  **il radar si giudica solo con SRI/ARPA**; la CUM3 è "pioggia osservata" e il suo
  accordo col pluviometro è in parte circolare (r≈0.96 sugli episodi, ott-2026).
- **Orari Socrata ARPA = ora solare (UTC+1)** senza fuso nella stringa. Query e lettura
  vanno convertite (ground_collect, verifica, previsioni). Dati archiviati corretti il 05/10/2026.
- **SIR Toscana (Monte di Fò) = giorno idrologico 09→09**: il valore datato D copre
  09:00 di D-1 → 09:00 di D (verificato: r con CUM3 0.61 → 0.98). Mai leggerlo come giorno civile.
- **CUM3 gap serale/notturno**: `collect.py` deve processare da IERI, non oggi,
  altrimenti i blocchi 21:00/24:00 non esistono ancora → cumulata mancante per
  eventi serali. Già corretto; non regredire.
- **forecast_verification.csv**: `bias_mm` = osservato − previsto (negativo = il modello
  sovrastima); l'osservato è la CUM3 (3h) → l'orizzonte 1h non è affidabile.
- **Fetch pluviometro Socrata**: usare filtro temporale `$where` sulla data, non
  `$limit` generico (con dati sub-orari copre solo ~14 giorni → eventi vecchi a 0).
- **Ruolo ARPA per area** (`ARPA_ROLE` in nowcast.py): Ruspino `'or'` (ARPA affidabile, r≈0.7);
  Cepina `'backup'` (ARPA quasi cieco, r 0.1–0.3: decide l'SRI DPC, ARPA solo se SRI assente o più
  vecchio di 20'). Coerente con `ARPA_TRUSTED` di episodes.py. Panna: niente ARPA.
- **Pre-allerta su tutti i frame nuovi**: per gli anelli 5/10 km il nowcast valuta il MASSIMO tra tutti i frame SRI
  arrivati dall'ultimo run (stato `<area>:nowcast:ring_last_ms:<km>`), non solo l'ultimo; testo e mappa usano l'ora
  di quel frame. Motivo: Ruspino 07/10/2026, anello a 10,7 e 11,6 mm/h in frame mai visti (run ogni ~20').
- **Latenza allerte**: il backtest (ott-2026) mostra che il ritardo sul picco a Ruspino è soprattutto
  latenza (run + cron GitHub), non soglia. Non allungare l'intervallo del nowcast oltre 10'.
- **MeteoSwiss ICON-CH1** (Open-Meteo `meteoswiss_icon_ch1`, oraria, dominio Alpi e dintorni, copre anche Panna):
  archiviato e verificato dal 10/2026; per scelta dell'utente (06/10/2026) è **terza fonte di conferma** nelle
  allerte forecast 6h (`monitor.py`) e 24h (`forecast_ensemble_alert.py`): OM/worst ≥ soglia E (MET o ICON-CH1 o
  media). Nella matrice (`forecast_matrix.py`) è solo mostrato (`meteoswiss_icon_seamless`, 72h), non cambia il
  livello. Decisione presa con pochi giorni di verifica: rivederla con `forecast_verification.csv` dopo 3–4 settimane.
- **Pluviometri ARPA in Previsioni**: `ground_daily.py` (workflow `ground-daily.yml`, ogni 3h) scrive `ground_daily.csv`
  (totali giorno UTC + n_obs/expected_obs). La pagina usa quello e chiama Socrata live solo se l'archivio ha buchi:
  dal browser Socrata risponde spesso **HTTP 429** (chiamate anonime limitate) → prima tutti i giorni erano "n.d.".
- **Mappa pre-allerta** (`alert_map.py`, dal 06/10/2026): dopo il testo Telegram della "cella in avvicinamento"
  parte la mappa JPEG 1024 px (~100–160 KB): radar sull'area ±30 km, poligono, anelli 5/10 km, frecce di moto.
  Ruspino e Cepina: album di 2 foto, radar DPC + radar MeteoSwiss (ultimo frame STAC, max 25' di età; se manca
  parte solo la DPC). Sfondo pre-renderizzato in `archive/data/basemaps/` dal workflow manuale `basemaps.yml`
  (stile `osm` zoom 11 o `topo` OpenTopoMap; nessuna tile a runtime). Se la mappa fallisce il testo è già partito.
  Spegnibile con env `NOWCAST_PREALERT_MAP=0`.
- **Moto cella nella pre-allerta**: `track_cell_motion()` (cross-correlazione 15', finestra centrata sulla cella).
  Il vecchio `estimate_motion()` (baricentro nell'anello) con una cella che ENTRA nell'anello può dare la direzione
  opposta (test sintetico ott-2026): resta solo come ripiego. Il moto NON decide il trigger, solo testo/mappa/prob.
  `track_field_motion()` = moto della PERTURBAZIONE d'insieme (finestra ~200 km, blocchi 4 km, ultimi 25',
  pioggia ≥ 0.2 mm/h; scartato se correlazione < 0.3): freccia larga "Perturbazione da …" nella mappa e riga in
  didascalia. Entrambi i tracker usano `_xcorr_motion()` (cross-correlazione normalizzata per sovrapposizione).
  Il moto della CELLA vale solo se corr ≥ 0.5, ≤ 90 km/h e coerente con la perturbazione (`cell_motion_reliable`:
  scarto ≤ 90° e velocità ≤ max(2,5×, +40 km/h)); altrimenti testo e prob. usano il moto d'insieme ("perturbazione")
  e la freccia sottile non si disegna. Caso reale Panna 07/10/2026: cella "SE 128 km/h" con perturbazione da W 28.
  `prealert_motion()` calcola tutto una volta; `estimate_motion()` resta solo nei messaggi "cella sull'area".
- **Radar MeteoSwiss** (`mch_collect.py`, `<area>_mch*.csv`): open data CC BY 4.0, citare "Fonte: MeteoSwiss";
  STAC libero solo 14 giorni → si archivia noi. Dati di STUDIO, non votano nelle allerte. Solo Ruspino e Cepina
  (Panna fuori copertura), archivio dal 22/09/2026. Confronti: `episodes.csv` colonne `mch_max_mm/mch_mean_mm/
  mch_cov_pct` (non decidono episodi né fonte), `mch_daily.csv` (totali giorno UTC, `mch_daily.py` in ground-daily.yml)
  → osservato "Radar MeteoSwiss" in Previsioni, colonne in Verifica, stime in Report.
- **Allerte forecast 24h** (`forecast_ensemble_alert.py`): una sola notifica per run, il livello PIÙ ALTO confermato
  (gli inferiori diventano attivi in silenzio), come `monitor.py`. Prima partivano ALARM+EMERGENCY insieme.
- **Matrice SP3 (soglie 24/48/72h Ruspino) è riservata**: non aggiungerla in pagine/testi del repo pubblico.
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
