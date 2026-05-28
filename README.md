# 🌧️ Radar DPC — Piattaforma di monitoraggio e allertamento piogge

Piattaforma web per la visualizzazione dei dati radar della Protezione Civile
Italiana (DPC), l'archiviazione storica e l'allertamento automatico via
**email + Telegram** per aree di studio specifiche (bacini sorgentiferi).

🔗 **Live**: https://ebbenetriglav.github.io/radar-dpc/

---

## Cosa fa

1. **Visualizza** i prodotti radar DPC in tempo reale su mappa interattiva.
2. **Archivia** ogni giorno i dati di pioggia delle aree di studio, costruendo
   un database storico nel repository.
3. **Allerta** automaticamente quando la pioggia osservata o prevista supera
   soglie configurabili, via email e Telegram.

Gira **gratis** su GitHub Pages (frontend) + GitHub Actions (backend
schedulato) + un Cloudflare Worker (proxy CORS per i dati S3 del DPC).

---

## Le 4 pagine web

| Pagina | File | Cosa mostra |
|--------|------|-------------|
| 🗺 **Mappa Live** | `index.html` | Overlay radar, player temporale, grafici per punto, export CSV/XLSX. Le 3 aree pre-caricate come punti. |
| 📊 **Storico** | `storico.html` | Visualizzazione storica dei prodotti radar. |
| 🗄 **Archivio** | `archivio.html` | Grafici CUM24/CUM3 archiviati, mini-mappa con arealizzazione IDW, overlay forecast OpenMeteo. |
| 🚨 **Monitor** | `monitor.html` | Stato real-time aree: ultima osservazione, soglie editabili, grafici DPC+OpenMeteo+MET Norway, eventi. |

---

## Aree monitorate

| Area | Località | Prodotti |
|------|----------|----------|
| **Ruspino** | Val Cavallina (BG), Lombardia | SRT1, CUM3 |
| **Panna** | Mugello (FI), Toscana | SRT1, CUM3 + ensemble 24h |
| **Cepina (Levissima)** | Valtellina (SO), Lombardia | SRT1, CUM3 |

Definite in `archive/areas.json` con poligono del bacino, 5 vertici campione e
configurazione `monitoring`.

---

## I 4 sistemi di allertamento

### 1. 🔴 Monitoraggio osservato (`monitor.py`) — ogni 15 min
Scarica ultimo **SRT1** (1h) e **CUM3** (3h), media sul poligono vs soglie.
Allerta sulla pioggia **già caduta**.

| Area | SRT1 (1h) warn/alarm/emerg | CUM3 (3h) warn/alarm/emerg |
|------|-----------|-----------|
| Ruspino | 10 / 20 / 30 | 15 / 30 / 50 |
| Panna | 5 / 10 / 15 | 10 / 15 / 20 |
| Cepina | 5 / 15 / 30 | 10 / 15 / 40 |

Soglie editabili dal Monitor (**Modifica soglie**); per attivarle sulle
notifiche, esportarle e committarle in `areas.json`.

### 2. 🔮 Forecast 6h con doppia conferma (`monitor.py`)
Confronta **OpenMeteo** e **MET Norway**. Alert solo se **ENTRAMBI** superano
la soglia (1h→SRT1, 3h→CUM3): elimina i falsi positivi da singolo modello.
Se più livelli scattano insieme, notifica solo il **più alto**.

### 3. ⛈️ Nowcasting radar (`nowcast.py`) — ogni 60 min
Il più affidabile per il preavviso immediato: usa **dati radar osservati**.
Due anelli buffer (**5 km e 10 km dal bordo del poligono**) per rilevare celle
in avvicinamento.
- Soglie (tutte le aree): SRI 10/15/25 mm/h · SRT1 8/15/20 mm/1h
- Calcola posizione cella, **direzione di moto** (8 punti cardinali) +
  **velocità km/h** (2 frame SRI), **probabilità di arrivo** sul bacino.
- Riporta intensità mm/h e cumulata 3h nel buffer.

### 4. 📊 Ensemble forecast 24h pesato (`forecast_ensemble_alert.py`) — ogni 6h
Specifico per **Panna**. Media pesata di **5 modelli** (ICON DWD, IFS ECMWF,
GFS NOAA, ARPEGE, AROME Météo-France) su **11 punti** con pesi per
quota/esposizione. Allerta se la cumulata prevista **prossime 24h** supera
10 / 15 / 20 mm.

---

## Architettura

Il browser **non può** chiamare direttamente S3 (manca CORS): passa dal
**Cloudflare Worker** (`cloudflare-worker/worker.js`) che aggiunge i CORS e
inoltra. Gli script Python in Actions chiamano il DPC direttamente.

```
radar-dpc/
├── index.html, storico.html, archivio.html, monitor.html
├── css/style.css
├── js/                        # config, api, georaster-utils, colormap,
│   └── ...                     #   player, location, chart-panel, alerts,
│                               #   timezone, basemap-picker, archive-tab, main
├── archive/
│   ├── areas.json             # poligoni, vertici, soglie
│   ├── scripts/
│   │   ├── collect.py                  # archiviazione
│   │   ├── monitor.py                  # osservato + forecast 6h
│   │   ├── nowcast.py                  # nowcasting radar buffer
│   │   ├── forecast_ensemble_alert.py  # ensemble 24h Panna
│   │   └── requirements.txt
│   ├── data/                  # CSV/XLSX + events.csv + last_observations.json
│   └── state/                 # stati anti-spam
├── .github/workflows/
│   ├── pages.yml  archive-daily.yml  monitor.yml  nowcast.yml  forecast-alert.yml
└── cloudflare-worker/worker.js
```

---

## Workflow automatici (GitHub Actions)

| Workflow | Frequenza | Cosa fa |
|----------|-----------|---------|
| `archive-daily.yml` | ogni 6h | Archivia CUM24 + CUM3 |
| `monitor.yml` | ogni 15 min | Osservato + forecast 6h doppia conferma |
| `nowcast.yml` | ogni 60 min | Celle radar buffer 5/10km + moto + probabilità |
| `forecast-alert.yml` | ogni 6h (xx:30) | Ensemble 24h pesato Panna |

Cron best-effort (possibili ritardi 5-15 min). Lanciabili a mano da
**Actions → [workflow] → Run workflow** (molti hanno `dry_run`). I dati
prodotti vengono committati automaticamente dal bot e letti dal frontend come
file statici.

---

## Setup notifiche

7 secrets in **Settings → Secrets and variables → Actions**:

| Secret | Valore |
|--------|--------|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Gmail mittente |
| `SMTP_PASS` | App Password Gmail (16 char, serve 2FA) |
| `SMTP_TO` | destinatari separati da virgola |
| `TELEGRAM_TOKEN` | token bot @BotFather |
| `TELEGRAM_CHAT_ID` | chat ID (da `getUpdates`) |

Guida: `archive/SETUP-NOTIFICHE.md`. Per aggiungere destinatari, aggiornare
`SMTP_TO` reinserendo la lista completa (il valore salvato non è visibile).

**Anti-spam**: ogni soglia notifica una sola volta alla salita; si riarma
quando il valore scende sotto il 50% per 30 min. Stato in `archive/state/`.

---

## Struttura dei dati

**CSV archivio** (long format, 1 riga per osservazione area/vertice):
`timestamp_utc, product, area_name, location_type, location_name, lat, lon,
value, mean, min, max, pixel_count, fetched_at_utc`

**XLSX** `{area}.xlsx`: sheet `*_raw` (long) + `*_pivot` (wide, pronto da
graficare).

**`events.csv`**: log eventi (osservati/forecast/nowcast) con livello, soglia,
valori, stato invio email/Telegram.

**`last_observations.json`**: snapshot per le dashboard (ultime osservazioni,
forecast OpenMeteo+MET Norway, heartbeat nowcast).

---

## Note tecniche

- **Proiezioni**: prodotti istantanei (VMI/SRI/SRT1…) in TM custom
  (`lat_0=42, lon_0=12.5`); cumulate (CUM3/CUM24…) in WGS84. Frontend
  riproietta con proj4; Python con rasterio/pyproj.
- **API DPC**: solo ~7 giorni rolling; lo storico oltre esiste solo grazie a
  questo archivio.
- **Bucket S3**: il nome può cambiare lato DPC; il Worker usa whitelist a
  pattern `*dpc-radar*.amazonaws.com`.
- **Fonti**: radar/cumulate © Protezione Civile; forecast OpenMeteo (15-min) e
  MET Norway (orario); ensemble 24h da OpenMeteo multi-model.

---

*Dati radar e cumulate: © Dipartimento della Protezione Civile.*
