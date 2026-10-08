# CHANGELOG — Piattaforma Web V2 Radar DPC

## 2026-10-08 (c) — Mappa live: 403 dal proxy Cloudflare
- Il `worker.js` ricopiato su Cloudflare era la copia vecchia del repo: l'allowlist non conteneva il bucket
  attuale `s3-prod-dpc-radar` del DPC → ogni GeoTIFF "403 Hostname not allowed", mappa live vuota.
  `cloudflare-worker/worker.js` ora ha bucket attuale + pattern `*dpc-radar*` + watchdog; tolte le copie
  duplicate (`worker-proxy.js`, `js/worker.js`) per non ricaderci.

## 2026-10-08 (b) — Sito pubblico anonimo, Cornalita
- **Nomi**: l'area toscana diventa `scarperia` in tutto il repo (file dati, basemap, eventi, episodi, stato;
  migrazione con `archive/scripts/migrate_rename_area.py`). Tolti i riferimenti a marchi e all'uso finale.
- **Destinatari fuori dal repo**: email e chat Telegram passano dal secret `AREAS_PRIVATE`
  (`area_private.py`), che contiene anche il nome usato nei messaggi privati. Senza secret le allerte vanno ai
  destinatari di default (SMTP_TO / TELEGRAM_CHAT_ID).
- **Pagina Monitor rimossa** dal sito; `monitor.py`, workflow e dati restano.
- **Livelli neutri** sulle pagine (Eventi, Verifica, tab Allerte): "Liv. 1/2/3", colori smorzati (`js/levels.js`).
- **Pluviometro Cornalita**: Socrata da Actions risponde spesso 429 → retry con attesa, token opzionale
  `SOCRATA_APP_TOKEN`; le finestre evento archiviate con misure mancanti (ARPA pubblica in ritardo) vengono
  ricontrollate per 72 h invece di restare sottostimate.

## 2026-10-08 — Previsione 6h: un solo messaggio
- Scarperia 08/10 00:25: due Telegram nello stesso minuto (SRT1 ALARM + CUM3 WARNING). La regola "solo il livello più alto"
  valeva per prodotto, non per area. Ora un unico messaggio con entrambi i prodotti, titolo al livello più alto.
- Stesso caso alle 02:07: un terzo messaggio "WARNING SRT1" mentre la previsione calava da ALARM. Ora, quando scatta
  un livello, quelli inferiori già superati diventano attivi senza messaggio; le escalation successive partono come prima.

## 2026-10-07 (e) — Ruspino: soglia cella 7 mm/h + allerta pioggia persistente
- **Cella sull'area a Ruspino: 10 → 7 mm/h** (`monitoring.cell_on_area_mmh` in areas.json; vale per SRI DPC e ARPA in
  OR). Backtest ARPA 06/06–07/10: da 7,8 a 8,8 avvisi/mese, nessun falso in più, 14/14 episodi ≥ 20 mm presi.
- **Nuova allerta "pioggia persistente"**: le soglie in mm/h non vedono la pioggia debole ma lunga (6 mm/h per 12 h =
  72 mm senza mai superare 7–10 mm/h). Cumulata mobile del radar DPC sull'area, a ogni run del nowcast:
  ≥ 15 mm/3h warning · ≥ 30 mm/6h alarm · ≥ 50 mm/12h emergency (configurabili in areas.json, per ora solo Ruspino).
  Con 6 mm/h costanti: warning dopo ~2,5 h, alarm ~5 h, emergency ~8 h. Email + Telegram come le altre allerte.

## 2026-10-07 (d) — Pre-allerta: niente picchi persi tra un run e l'altro
- Ruspino 07/10 sera: pioggia debole-moderata sull'area (max 3,5 mm/h SRI, 6 ARPA, 4,6 MeteoSwiss: sotto le soglie,
  nessuna allerta "cella sull'area" corretta), ma nell'anello 10 km il radar DPC ha toccato 10,7 mm/h (20:45 UTC) e
  11,6 (21:15) in frame che il nowcast non ha mai valutato: girava ogni ~20' e guardava solo l'ultimo frame.
- Ora la pre-allerta valuta il massimo di tutti i frame arrivati dall'ultimo run (fino a 30'); il messaggio riporta
  l'ora del frame del picco. Soglie e canali invariati (solo Telegram).

## 2026-10-07 (c) — Scheduler Cloudflare: meno falsi allarmi
- Il 07/10 GitHub ha risposto più volte HTTP 500 al riavvio dei workflow ("avvio FALLITO (500)"), mentre i workflow
  giravano regolarmente col cron (nessun buco > 30 min nei commit). Ora `worker-scheduler.js` ritenta fino a 3 volte
  sui 5xx e manda il Telegram solo se il workflow è davvero in ritardo (> 2 intervalli) o se l'errore è 4xx
  (token/configurazione). Va ricopiato a mano nel Worker su Cloudflare.

## 2026-10-07 (b) — Mappa pre-allerta: moto cella affidabile, MeteoSwiss, sfondo più leggibile
- **Moto della cella**: la mappa di Scarperia del 07/10 indicava "cella → SE 128 km/h" con la perturbazione da W a 28 km/h:
  il tracciamento era saltato su un'altra cella. Ora il moto della cella si usa solo se plausibile (correlazione ≥ 0,5,
  ≤ 90 km/h) e coerente con la perturbazione; altrimenti freccia sottile omessa e testo/probabilità usano il moto
  d'insieme ("Movimento (perturbazione)"). Tolto il ripiego sul baricentro nell'anello per la pre-allerta.
- **Ruspino e Cepina: 2 immagini** (album Telegram): radar DPC + radar MeteoSwiss (ultimo frame, stessi riferimenti).
  Se MeteoSwiss non risponde parte solo la mappa DPC.
- **Sfondo più leggibile**: mappe a 1024 px, tile zoom 11 (nomi e strade nitidi), colori meno sbiaditi; nuovo stile
  `topo` (OpenTopoMap, rilievo). Da rigenerare con l'action "Sfondi mappe pre-allerta" scegliendo lo stile.

## 2026-10-07 — Allerte 24h senza doppioni + radar MeteoSwiss nei confronti
- **Forecast 24h**: con più soglie superate nello stesso run partivano due messaggi (Scarperia 07/10: ALARM + EMERGENCY con
  gli stessi numeri). Ora parte solo il livello più alto, come per le allerte 6h; escalation e riarmo invariati.
- **Radar MeteoSwiss come osservato** (Ruspino e Cepina, dal 22/09): nuova opzione in Previsioni (media area, pixel max
  nella tessera, "parziale" se mancano frame); `mch_daily.py` → `mch_daily.csv` ogni 3 ore (workflow ground-daily).
- **Verifica**: colonne "Radar MeteoSwiss" e "Δ MeteoSwiss" per episodio. **Report**: righe MeteoSwiss nella tabella
  stime vs pluviometro, con avviso "campione piccolo" sotto 10 episodi. `episodes.csv`: colonne `mch_*` in coda.

## 2026-10-06 (e) — Mappa pre-allerta: freccia della perturbazione
- Oltre alla cella più intensa, la mappa mostra il **moto d'insieme della perturbazione**: freccia larga blu che entra
  dal lato di provenienza e punta verso l'area ("Perturbazione da SW · 30 km/h"); la didascalia Telegram lo scrive.
- `track_field_motion()`: cross-correlazione su ~200 km attorno all'area, ultimi 25', a blocchi di 4 km. Se la
  correlazione è bassa (< 0.3) la freccia non compare: "moto d'insieme non determinabile".
- Corretta una distorsione della cross-correlazione (sottostimava la velocità): ora normalizzata per sovrapposizione.

## 2026-10-06 (d) — Mappa radar nella pre-allerta Telegram
- Nuovo `alert_map.py`: dopo il messaggio "cella in avvicinamento" arriva su Telegram una mappa (640 px, ~40–80 KB):
  radar DPC SRI, area, anelli 5/10 km, cella più intensa, freccia di spostamento stimato in 30', scala, legenda.
- Sfondo OpenStreetMap schiarito generato una volta per area (`basemaps.yml`, manuale) in `archive/data/basemaps/`.
- Moto della cella: nuovo tracciamento per cross-correlazione sugli ultimi 15' (`track_cell_motion`). Il metodo
  precedente sbagliava direzione proprio con celle in arrivo (test sintetico: NE stimato N/W). Cambia la riga
  "Movimento" e la probabilità di arrivo nel testo; il trigger NON cambia.
- Robustezza: se la mappa non si genera o non parte, il testo resta inviato (log "mappa pre-allerta non generata").

## 2026-10-06 (c) — Pluviometri ARPA: workflow dedicato
- In Previsioni Socrata risponde **HTTP 429** (Too Many Requests) alle chiamate dal browser: il dato live non è affidabile.
- `ground_daily.py` spostato da forecast-verify (2×/giorno) a un workflow dedicato `ground-daily.yml` ogni 3 ore.
- La pagina chiama Socrata live solo se l'archivio ha giorni mancanti o parziali; messaggi d'errore più chiari.

## Sessione 2026-10-06 (b) — Pluviometro in Previsioni + MeteoSwiss ICON-CH1 nelle allerte

### Previsioni: pluviometro a terra sempre "n.d."
- Causa: la pagina leggeva ARPA Socrata dal browser; se la chiamata fallisce (rete, CORS, lentezza) tutti i giorni
  passati risultavano "n.d.", anche quelli asciutti (0 mm).
- Nuovo `ground_daily.py` (step in `forecast-verify.yml`): totali giornalieri UTC di Cornalita e Oga S.Colombano in
  `archive/data/ground_daily.csv`, con n. misure valide e attese. La pagina usa l'archivio e integra con Socrata live
  (timeout 12 s); per ogni giorno vince la fonte con più misure. Giorno con misure mancanti = "parziale" (totale minimo),
  mai zero inventato. La riga di stato dice da dove arriva il dato o perché manca.

### MeteoSwiss ICON-CH1 nelle allerte forecast (richiesta utente)
- `monitor.py` (6h): OpenMeteo ≥ soglia E (MET Norway **oppure** ICON-CH1 ≥ soglia). Prima: solo MET.
- `forecast_ensemble_alert.py` (24h): worst-case ≥ soglia E (media **oppure** MET **oppure** ICON-CH1). ICON-CH1
  pesato sui punti solo se tutti i punti hanno il dato; mai mediato nell'ensemble.
- Messaggi: valore MeteoSwiss sempre mostrato + "confermato da …"; in `events.csv` nota con le fonti di conferma.
- `forecast_matrix.py`: MeteoSwiss ICON (seamless, 72h) mostrato come terzo riferimento, **non** cambia il livello.
- Effetto atteso: qualche allerta in più quando MET non conferma ma ICON-CH1 sì. Da verificare dopo 3–4 settimane.
- Fix: titolo Telegram del forecast 24h diceva sempre "Scarperia".

## Sessione 2026-10-06 — Pagina Radar MeteoSwiss + revisione di tutte le pagine

### Nuova pagina 🛰 Radar MeteoSwiss (`mch.html`)
- Mappa live (ultima ora, player, LIVE ogni 5') con aree Ruspino/Cepina e radar Lema/Weissfluhgipfel/Albis; legenda mm/h;
  grigio = fuori copertura (nessun dato, non zero). Schede area: max/media sull'area, anello 10 km, pioggia 24 h
  MeteoSwiss vs DPC SRI vs ARPA **sugli stessi istanti**, grafico 24 h. "Dato di studio, non vota nelle allerte" · CC BY 4.0.
- `mch_collect.py`: ogni frame recente → PNG in `archive/data/radar_mch/` (Web Mercator, ultimi 12) + `index.json`;
  `arpa-collect.yml` li committa. Link nel menu di tutte le pagine.

### Correzioni dati (cosa mostrava numeri o stati sbagliati)
- **Monitor**: lo stato area ignorava gli eventi nowcast/cella su area e lasciava che una previsione accendesse "ALARM"
  → stato osservato da soglie + nowcast + cella attiva; previsioni in un badge separato "PREVISTO". Valori evento con unità
  giuste (SRI/ARPA = max mm/h), "NaN" → "—", note con virgole lette bene. Grafico: finestra reale (3h = ±3h), tutto in mm/h,
  dati mancanti = vuoto. Riga "Nowcast: ultimo run …" (rossa se > 20'). CUM3/CUM24 ora si aggiornano davvero.
- **Mappa Live**: id duplicati delle aree preimpostate (Cepina mostrava i dati di Scarperia); crash del grafico; stato verde
  "Aggiornato" anche con API DPC in errore; scheda Allerte con soglie inventate → soglie di `areas.json` + nota "indicativo".
- **Storico**: CUM24 ora = giorno UTC esatto (prima un campione alle 12 UTC); CUM3/6/12 dichiarati "blocco che termina alle
  12 UTC"; tabella risultati che non compariva; giorno in più; CSV scaricato due volte; "pluviometri, non radar".
- **Archivio**: barre CUM24 spostate di un giorno; 121 blocchi CUM3 mancanti dichiarati (totale "incompleto"); previsione
  allineata ai blocchi 3h; animazione sul giorno completo; mappa IDW etichettata come stima, scala fissa.
- **Eventi**: note con virgole spezzate; conteggi senza le chiusure; filtri forecast24h/nowcast; unità mm vs mm/h.
- **Verifica**: badge forecast allineato a quando il server invia davvero (dal primo livello); Scarperia/Cepina per giorno
  civile come il server; crash vista "Tutto"; CUM3 disegnata 3 h in ritardo; pluviometro mancante nel grafico evento;
  aggiunta serie SRI DPC (e MeteoSwiss, nascosta); unità per prodotto; fonti con < 30 confronti "campione insufficiente";
  bias = previsto − osservato su Verifica e Report.
- **ARPA**: il testo diceva "nessun alert su questi dati" (falso: ARPA vota a Ruspino e fa da riserva a Cepina); confronto
  "CUM3 pluviometri vs ARPA" con copertura n/36.
- **Report**: grafico episodi con barre invisibili; KPI "allerte non agganciate a un episodio".

### UX
- Tema chiaro/scuro coerente su tutte le pagine (chiave `radar-theme`), grafici leggibili in tema chiaro; menu scorrevole su
  telefono; "ultimo aggiornamento" su Archivio, Eventi, Monitor; numeri lunghi nelle note arrotondati.

## Sessione 2026-10-06 — "Cella in avvicinamento" di nuovo attiva

- 🐞 `nowcast.py` `_eval_product` — gli avvisi "cella radar in avvicinamento" (anelli 5/10 km, SRI e SRT1)
  **non si riarmavano mai**: ogni livello restava 'active' per sempre dopo il primo invio → 33 avvisi a giugno,
  6 a luglio, 1 ad agosto (08/08), poi silenzio. Ora il livello si riarma se il segnale è sotto soglia e sono
  passate `NOWCAST_REARM_H` (3) ore dall'ultimo invio; gli stati bloccati da giugno si sbloccano al primo run asciutto.
- ✏️ Anti-raffica: al massimo un messaggio "cella in avvicinamento" per area ogni 3 h tra anelli e prodotti,
  salvo salita di livello (warning → alarm → emergency). Soglie e canali invariati.
- 🔎 Backtest anello continuo 07/07→06/10 (SRI ≥ 10 mm/h entro 10 km, riarmo 3 h): Ruspino ~9 avvisi/mese,
  5 senza pioggia sull'area in 3 mesi, 10/12 episodi significativi presi; Cepina ~16/mese; Scarperia ~10/mese.

## Sessione 2026-10-06 — Radar MeteoSwiss in archivio (studio)

- 🆕 `mch_collect.py` — radar MeteoSwiss PRECIP (RZC, mm/h ogni 5', open data CC BY 4.0 "Fonte: MeteoSwiss")
  dagli item STAC giornalieri `ch.meteoschweiz.ogd-radar-precip`. Per area: max/media nel poligono,
  max e % sopra 5 mm/h nell'anello 10 km, **% pixel senza dato** (bordo copertura dichiarato, mai zeri inventati).
  Georeferenziazione letta dal file (projdef + angolo UL). Nessuna allerta lo legge.
- ⚙️ `arpa-collect.yml` — nuovo passo (continue-on-error) → `<area>_mch.csv` ogni 10'; timeout job 8→12'.
- 🆕 `mch-radar.yml` (manuale): `probe` (struttura file + stats, solo log) e `backfill` (14 giorni del STAC
  → `<area>_mch_backfill.csv`). `requirements.txt` + h5py.
- 📌 Da fare: confronto coi pluviometri Cornalita e Oga dopo 3–4 settimane (ipotesi: a Cepina vede meglio di DPC/ARPA).

## Sessione 2026-10-06 — Backfill che si riavvia da solo

- ⚙️ `sri-backfill.yml` — il titolo del run porta i parametri (`mode= round= retry= zero= frames= auto=`).
  Un giro a 0 frame (DPC che non risponde) viene ritentato fino a 2 volte dopo 5'; prima la catena si fermava.
- 🆕 `backfill-watchdog.yml` — ogni 30' guarda l'ultimo run del backfill: se è **fallito** (es. 05/10 22:04,
  "job was not acquired by Runner" = guasto GitHub) lo rilancia con gli stessi parametri, max 5 tentativi di
  fila. Non tocca i run annullati a mano (Cancel = stop voluto).

## Sessione 2026-10-06 — Pagina Previsioni rifatta

- 🆕 `previsioni.html` — tre fonti: **Ensemble** Open-Meteo (5 modelli), **YR** (MET Norway, dal browser
  come in verifica) e **MeteoSwiss** (`meteoswiss_icon_seamless`: ICON-CH1 poi CH2, fino a +5 g, anche i 7 giorni
  passati). Selettore Modelli (Tutti / Ensemble / YR / MeteoSwiss) e selettore Osservato (pluviometro / CUM3 / ARPA).
- 📊 Grafici: cumulata osservato→previsto con linea "adesso" e fascia min–max dei modelli; nuovo grafico
  **intensità oraria 48 h**; passato = barre osservato + simboli previsto; futuro = barre per fonte con baffo
  worst-case; striscia 15 giorni con mini-barre; tessere del giorno; tabella dati. Colori fissi per fonte,
  validati per daltonismo (dark e light); tema chiaro ora funzionante (prima il pulsante non agiva).
- 🐞 Worst-case: era la somma ora per ora del massimo tra i modelli (gonfiato, es. 80 mm); ora è il
  **singolo modello più piovoso**, come nelle allerte. Modelli che non coprono un'ora/giorno (AROME oltre
  ~2 g) erano contati come 0 (`isFinite(null)`): ora esclusi. AROME allineato a `arome_france_hd` (come gli script).
- ✏️ CUM3 etichettata "pluviometri interpolati" (non "radar") e sommata come **media** d'area (era il pixel max).
  Oggi = solo le ore che restano (prima: giorno intero, non confrontabile con YR).

## Sessione 2026-10-06 — Pre-allerta anello: primo test

- 🔎 Backtest anello 10 km Ruspino (3.134 frame SRI, finestre [inizio−3h, picco] di 43 episodi, 16
  significativi ≥ 20 mm). Regola "cella ≥ 10 mm/h nell'anello": presi 16/16, anticipo mediano sul picco
  80' (allerta reale oggi: −3'; stessa soglia dentro l'area a ritardo zero: 20') → **+22' veri** rispetto
  all'area a parità di ritardo. Costo: scatta in 22/27 episodi non significativi (oggi 16/27) e in un
  numero NON ancora misurato di passaggi di celle vicine senza pioggia sull'area.
- 🆕 `sri_collect.py --backfill-ring-days` (+ modalità `backfill-ring-days` in `sri-backfill.yml`):
  anello continuo dal 15/05 per tutte le aree, per misurare quei falsi allarmi. Frame per giro 1000
  (era 400), timeout 45', catena fino a 80 giri.

## Sessione 2026-10-06 — MeteoSwiss ICON-CH1 in verifica

- 🆕 `monitor.py` — `fetch_forecast_meteoswiss`: ICON-CH1 (1 km, run ogni 3h) via Open-Meteo
  (`models=meteoswiss_icon_ch1`), max 1h/3h nelle prossime 6h, salvato in `last_observations.json`
  come `forecast.meteoswiss`. **Solo verifica**: NON entra nella doppia conferma (OpenMeteo + MET Norway
  invariata). Fuori dominio del modello (es. Scarperia se non coperta) → nessun valore.
- ✏️ `forecast_history.py`, `forecast_verify.py` — terza fonte `meteoswiss` archiviata e verificata
  come le altre; `verifica.html` e `report.html` la mostrano come "MeteoSwiss ICON-CH1 (solo verifica)".
- ⚙️ `sri-backfill.yml` — backfill **automatico**: con "continua" attivo (default) il workflow si
  rilancia da solo (`gh workflow run`, GITHUB_TOKEN + `actions: write`) finché restano frame e il giro
  ha archiviato qualcosa; un giro a 0 frame (frame oltre la memoria API DPC) o 25 giri fermano la catena.
  `sri_collect.py` scrive `progress`/`remaining` in `GITHUB_OUTPUT`.
- 📌 Dopo 3–4 settimane di confronti: decidere se usarlo nelle allerte (es. OpenMeteo E (MET Norway O MeteoSwiss)).

## Sessione 2026-10-05 (notte) — Cepina radar DPC, anticipo Ruspino, SP3

### Allerte Cepina — radar DPC primario, ARPA di riserva
- ✏️ `nowcast.py` — nuovo `ARPA_ROLE = {'ruspino': 'or', 'cepina': 'backup'}`. A Cepina il radar ARPA
  è quasi cieco (r 0.1–0.3 col pluviometro Oga): "cella sull'area" decide l'**SRI DPC**; ARPA entra solo
  se l'SRI manca o è più vecchio di `SRI_MAX_AGE_MIN` (20'), con messaggio "ARPA di riserva".
  Se il DPC non risponde affatto, `_arpa_only_cell_check` valuta comunque ARPA (prima il run usciva).
  Ruspino invariato (DPC **oppure** ARPA).
- ✏️ `monitor.py` — la riga di conferma ARPA nei messaggi solo per Ruspino.
- ✏️ `nowcast.py` — testo allerta: "Pioggia caduta finora (CUM3 pluviometri, 3h)" (prima diceva "radar").

### Anticipo Ruspino
- 🔎 Backtest 16 episodi significativi (≥ 20 mm): prima allerta mediana 3' **dopo** il picco; il dato
  radar a 5' supera già 10 mm/h ~20' prima del picco → la perdita è soprattutto **latenza**
  (attesa del run ogni 20' + ritardo cron GitHub). Soglie più basse sul solo poligono rendono poco
  a parità di latenza (le celle passano da 3 a 10 mm/h in 5–10').
- ✏️ `nowcast.yml` — ogni 10' (era 20'); `worker-scheduler.js` `every: 10`, `worker.js` maxAge 25'
  (da ri-deployare a mano su Cloudflare).
- 🆕 `sri_collect.py` — anello 10 km attorno a ogni area (area esclusa): `<area>_sri_ring.csv`
  (continuo) e `--backfill-ring` → `<area>_sri_ring_backfill.csv` (finestre [inizio−3h, inizio+3h] degli
  episodi Ruspino). Dati di STUDIO per la pre-allerta "cella in arrivo": nessuna allerta li legge.
  `sri-backfill.yml` ha la nuova modalità `backfill-ring`.

### Privacy
- 🔒 `report.html` — tolto il confronto con la matrice SP3 (repo pubblico). Restano da gestire
  `forecast_matrix.py` e la sezione 24/48/72h di `verifica.html` (vedi migrazione).

## Sessione 2026-10-05 — Episodi di pioggia + mappa

### Mappa
- ✏️ `js/basemap-picker.js`, `js/georaster-utils.js`, `arpa.html`, `js/archive-tab.js`, `js/config.js` —
  CARTO ora richiede API key (watermark): sostituito con Esri Gray Canvas (scuro/chiaro, no key).
  Il radar ha un pane dedicato (z 350) sopra qualunque basemap; etichette Esri sopra il radar.

### Episodi Cepina (05/10 notte)
- 🐞 `episodes.py` — a Cepina il radar ARPA (quasi cieco: r≈0.2 col pluviometro) "cancellava" la pioggia
  vista da SRI e CUM3. Nuovo `ARPA_TRUSTED = ('ruspino',)`: dove ARPA non è affidabile l'SRI conta sempre
  e i frame ARPA asciutti non smentiscono la CUM3; il picco viene dall'SRI. Effetto: episodio 20/08 da 10 a
  19,5 h (pluvio 55 mm), allerte senza episodio 45 → 28. MIT 3h confermato (`--calibrate` ora usa anche l'SRI).

### Report (05/10 sera)
- 🆕 `report.html` (menu "📑 Report" su tutte le pagine) — report generato nel browser dai dati del repo,
  per area e periodo: **Sintesi stakeholder** (KPI, testo automatico, stato per area, episodi principali,
  "Copia sintesi" per email) e **Tecnico** (stime radar/CUM3 vs pluviometro, allerte sugli episodi
  significativi, previsioni 3h, elenco episodi con descrizione automatica). Stampa/PDF e CSV.
  Numeri calcolati dal codice, frasi standard; analisi ragionata su richiesta.
- ✏️ `verifica.html` — etichetta bias corretta: `bias_mm` = osservato − previsto (negativo = sovrastima).

### Verifica dati (05/10 sera)
- 🐞 Pluviometro SIR Monte di Fò (Scarperia) letto come giorno civile: è **giorno idrologico 09→09**
  (valore datato D = 09:00 di D-1 → 09:00 di D). Corretti `episodes.py`, `verifica.html`, `previsioni.html`.
  Effetto: sparisce il falso "pluviometro a 0 mm mentre radar e CUM3 vedono 15–30 mm" (20/08, 09/09).

### Verifica dati (05/10 pomeriggio)
- 🔎 La CUM3 DPC **non è radar**: per la doc DPC le cumulate 3/6/12/24h sono ottenute solo dai
  pluviometri a terra interpolati. Il suo accordo col pluviometro (r≈0.96) è in parte circolare.
  Etichette corrette ovunque ("CUM3 pluviometri"); il radar si giudica solo con ARPA e SRI.
- 🐞 Orari pluviometri ARPA (Socrata) in **ora solare UTC+1**, letti come UTC → 1h di ritardo.
  Corretti `ground_collect.py`, lettura live in `verifica.html`/`previsioni.html` e i dati archiviati.
- 🆕 `archive/scripts/sri_collect.py` — archivio radar DPC SRI (5') per tutte le aree, Scarperia compresa
  (`<area>_sri.csv`), nel workflow `arpa-collect` ogni 10'. `--probe` verifica la profondità
  storica dell'API DPC; `.github/workflows/sri-backfill.yml` recupera i frame delle finestre episodio.
- ✏️ `episodes.py` / `verifica.html` — colonne "Radar DPC SRI" e "Δ SRI"; per Scarperia l'SRI
  definisce l'episodio a 5' (prima solo blocchi CUM3 3h).

### Episodi di pioggia (Verifica)
- 🆕 `archive/scripts/episodes.py` — raggruppa la pioggia in episodi (ARPA 5′, DPC CUM3 dove ARPA manca);
  un episodio si chiude dopo **3h asciutte** (MIT calibrato su Cornalita/Oga: `--calibrate`).
  Cumulate ARPA/DPC/pluviometro sulla stessa finestra; allerte agganciate (anticipo vs picco).
  Output derivati: `archive/data/episodes.csv`, `archive/data/episodes_alerts.csv` (events.csv intatto).
- ✏️ `archive/scripts/ground_collect.py` — archivia il pluviometro anche per gli episodi senza allerte.
- ✏️ `.github/workflows/forecast-verify.yml` — episodi → ground_collect → episodi, 2×/giorno (06/18 UTC).
- ✏️ `verifica.html` — nuova vista "episodi di pioggia" (default) con riga espandibile (allerte, anticipo,
  grafico); la vista per allerte resta disponibile.

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
  per check sanity vs DPC. Scarperia esclusa (fuori copertura).

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
