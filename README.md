# 📡 Radar DPC — Visualizzatore Meteo

Piattaforma web open source per la visualizzazione e l'analisi dei dati radar meteorologici della **Protezione Civile Italiana**, basata sull'[API REST DPC](https://dpc-radar.readthedocs.io/it/latest/).

🔗 **Demo live**: `https://<tuo-username>.github.io/radar-dpc/`

---

## ✨ Funzionalità

### 🗺 Mappa Live (`index.html`)
- **Overlay radar** su mappa interattiva (Leaflet + tile CARTO)
- **20+ prodotti** disponibili: VMI, SRI, CUM3/6/12/24, CAPPI 1–10 km, VIL, ETM, POH, TEMP
- **Animazione temporale** con play/pausa, slider, velocità regolabile
- **Pre-fetch in background** degli ultimi 24 fotogrammi
- **Scala colori** interpolata per ogni prodotto (dBZ, mm/h, mm, °C, …) con legenda dinamica
- **Opacità** overlay regolabile

### 📍 Punti di interesse
- **Ricerca geocoding** via OpenStreetMap Nominatim (no API key)
- **Inserimento coordinate** dirette (lat, lon)
- **Click su mappa** per aggiungere punti (max 3 contemporanei)
- **Buffer 2 km** — media statistica dei pixel nel raggio (Haversine)
- Visualizzazione **statistiche** (media, min, max, numero pixel)

### 📈 Grafico serie temporale
- Confronto fino a **3 punti** sullo stesso grafico (Chart.js)
- Export **CSV** dei dati estratti
- Storico accumulato durante la navigazione

### 🔔 Sistema allerte
- **Soglie configurabili** warn/danger per prodotto (es. SRI >30 mm/h)
- Notifiche visive nel pannello allerte
- **Browser Notifications** (con consenso utente)
- Log storico degli eventi

### 📊 Storico dati (`storico.html`)
- Selezione **range date** (max 90 giorni)
- Download **CUM24 giornaliero** per i punti selezionati
- Export **CSV** con data, media, min, max per ogni punto
- Log di esecuzione in tempo reale

---

## 🚀 Deploy su GitHub Pages

### 1. Fork / Clone
```bash
git clone https://github.com/<tu>/radar-dpc.git
cd radar-dpc
```

### 2. Abilita GitHub Pages
- Vai su **Settings** → **Pages**
- Seleziona **Source: GitHub Actions**
- La pipeline `.github/workflows/pages.yml` si occupa del deploy automatico

### 3. Push
```bash
git push origin main
```

Il sito sarà disponibile a `https://<username>.github.io/radar-dpc/` dopo qualche minuto.

---

## 🏗 Architettura

```
radar-dpc/
├── index.html          # Pagina principale (mappa live)
├── storico.html        # Download storico dati
├── css/
│   └── style.css       # Stili completi (dark theme)
├── js/
│   ├── config.js       # Prodotti, scale colori, costanti
│   ├── api.js          # Wrapper API REST DPC + geocoding
│   ├── colormap.js     # Interpolazione colori e legenda
│   ├── georaster-utils.js  # GeoTIFF → Leaflet + estrazione buffer
│   ├── player.js       # Animazione e time controls
│   ├── location.js     # Ricerca località, marker, buffer
│   ├── chart-panel.js  # Grafico serie temporale (Chart.js)
│   ├── alerts.js       # Sistema allerte soglie
│   ├── main.js         # Orchestrazione pagina principale
│   └── storico.js      # Logica pagina storico
└── .github/workflows/
    └── pages.yml       # Deploy automatico GitHub Pages
```

### Librerie utilizzate (tutte via CDN, no build step)
| Libreria | Versione | Uso |
|---|---|---|
| [Leaflet](https://leafletjs.com/) | 1.9.4 | Mappa interattiva |
| [geotiff.js](https://geotiffjs.github.io/) | 2.1.3 | Parsing GeoTIFF |
| [georaster](https://github.com/GeoTIFF/georaster) | 1.6.1 | Wrapper GeoRaster |
| [georaster-layer-for-leaflet](https://github.com/GeoTIFF/georaster-layer-for-leaflet) | 0.9.0 | Rendering GeoTIFF su Leaflet |
| [Chart.js](https://www.chartjs.org/) | 4.4.0 | Grafici serie temporale |
| [Font Awesome](https://fontawesome.com/) | 6.4.2 | Icone |

---

## ⚠️ Note tecniche

### CORS
L'API `radar-api.protezionecivile.it` ha CORS abilitato per `POST` e `OPTIONS`.
Le pre-signed URL S3 generalmente sono accessibili da browser. In caso di errori CORS
sui file GeoTIFF, il messaggio verrà mostrato nel pannello di stato.

### Proiezione
I file GeoTIFF DPC sono tipicamente in WGS84 (EPSG:4326) o UTM32N.
`georaster-layer-for-leaflet` gestisce la riproiezione automaticamente.

### Rate limiting
Nominatim (geocoding) accetta max 1 richiesta/secondo.
Il modulo storico rispetta un delay di 1.1s tra le richieste giornaliere.

---

## 📄 Licenza

MIT — Dati radar © [Dipartimento della Protezione Civile](https://www.protezionecivile.gov.it/)

---

## 🙏 Crediti

- Dati radar: **DPC — Dipartimento della Protezione Civile Italiana**
- API documentazione: [dpc-radar.readthedocs.io](https://dpc-radar.readthedocs.io/it/latest/)
- Geocoding: OpenStreetMap / Nominatim
