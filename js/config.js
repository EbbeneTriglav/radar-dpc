/**
 * config.js — Configurazione globale Radar DPC
 * Prodotti, scale colori, costanti API
 */

const CONFIG = {

  // ───────────────────────────────────────────────────────────────────────
  // CORS PROXY (Cloudflare Worker dedicato)
  // Serve a scaricare i file .tif dal bucket S3 di DPC che non ha CORS.
  // Se imposti la stringa vuota '' il codice in api.js userà la catena di
  // proxy pubblici di fallback (allorigins, codetabs, cors.sh).
  // ───────────────────────────────────────────────────────────────────────
  CORS_PROXY: 'https://radar-dpc-proxy.riccardo-giusti-gst.workers.dev/?url=',

  // WebSocket DPC: lasciare null finché non viene reso pubblico.
  // Quando null il sito usa il polling REST (auto-refresh ogni 60 s).
  WSS_URL: null,

  API: {
    BASE: 'https://radar-api.protezionecivile.it',
    LAST: '/findLastProductByType',
    DOWNLOAD: '/downloadProduct',
  },

  GEOCODING: 'https://nominatim.openstreetmap.org/search',

  MAP: {
    CENTER: [42.0, 13.0],
    ZOOM: 6,
    TILE_URL: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    TILE_ATTR: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OSM contributors',
    TILE_URL_LIGHT: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  },

  BUFFER_KM: 2,
  REFRESH_MS: 60_000,
  MAX_FRAMES: 24,

  // ───────────────────────────────────────────────────────────────────────
  // Prodotti disponibili sull'API DPC
  // ───────────────────────────────────────────────────────────────────────
  PRODUCTS: {
    VMI: {
      label: 'VMI – Riflettività verticale massima',
      unit: 'dBZ',
      stepMs: 300_000,
      category: 'base',
      colorScale: 'dbz',
      description: 'Valore massimo di riflettività radar sulla verticale. Indica la presenza e intensità dei precipitati.',
    },
    SRI: {
      label: 'SRI – Intensità precipitazione',
      unit: 'mm/h',
      stepMs: 300_000,
      category: 'base',
      colorScale: 'rain_rate',
      description: 'Stima dell\'intensità di pioggia istantanea al suolo.',
    },
    SRT1: {
      label: 'SRT1 – Cumulata oraria',
      unit: 'mm',
      stepMs: 300_000,
      category: 'base',
      colorScale: 'accumulation',
      description: 'Precipitazione cumulata nell\'ultima ora.',
    },
    IR_108: {
      label: 'IR 10.8 – Infrarosso satellitare',
      unit: 'K',
      stepMs: 900_000,
      category: 'altro',
      colorScale: 'temperature',
      description: 'Temperatura di brillanza canale IR 10.8 µm (Meteosat).',
    },
    CUM3: {
      label: 'CUM3 – Cumulata 3h',
      unit: 'mm',
      stepMs: 10_800_000,
      category: 'cumulate',
      colorScale: 'accumulation',
      description: 'Precipitazione cumulata nelle ultime 3 ore.',
    },
    CUM6: {
      label: 'CUM6 – Cumulata 6h',
      unit: 'mm',
      stepMs: 21_600_000,
      category: 'cumulate',
      colorScale: 'accumulation',
      description: 'Precipitazione cumulata nelle ultime 6 ore.',
    },
    CUM12: {
      label: 'CUM12 – Cumulata 12h',
      unit: 'mm',
      stepMs: 43_200_000,
      category: 'cumulate',
      colorScale: 'accumulation',
      description: 'Precipitazione cumulata nelle ultime 12 ore.',
    },
    CUM24: {
      label: 'CUM24 – Cumulata 24h',
      unit: 'mm',
      stepMs: 86_400_000,
      category: 'cumulate',
      colorScale: 'accumulation',
      description: 'Precipitazione cumulata nelle ultime 24 ore.',
    },
    TEMP: {
      label: 'TEMP – Temperatura',
      unit: '°C',
      stepMs: 3_600_000,
      category: 'altro',
      colorScale: 'temperature',
      description: 'Campo di temperatura al suolo.',
    },
    VIL: {
      label: 'VIL – Acqua liquida integrata',
      unit: 'kg/m²',
      stepMs: 300_000,
      category: 'storm',
      colorScale: 'vil',
      description: 'Vertically Integrated Liquid — indicatore di intensità temporalesca.',
    },
    ETM: {
      label: 'ETM – Echo Top',
      unit: 'km',
      stepMs: 300_000,
      category: 'storm',
      colorScale: 'etm',
      description: 'Altezza massima del sistema precipitante.',
    },
    POH: {
      label: 'POH – Probabilità grandine',
      unit: '%',
      stepMs: 300_000,
      category: 'storm',
      colorScale: 'probability',
      description: 'Probability Of Hail — stima della probabilità di grandine al suolo.',
    },
    CAPPI_1:  { label: 'CAPPI 1 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 1 km.' },
    CAPPI_2:  { label: 'CAPPI 2 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 2 km.' },
    CAPPI_3:  { label: 'CAPPI 3 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 3 km.' },
    CAPPI_4:  { label: 'CAPPI 4 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 4 km.' },
    CAPPI_5:  { label: 'CAPPI 5 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 5 km.' },
    CAPPI_6:  { label: 'CAPPI 6 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 6 km.' },
    CAPPI_7:  { label: 'CAPPI 7 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 7 km.' },
    CAPPI_8:  { label: 'CAPPI 8 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 8 km.' },
    CAPPI_9:  { label: 'CAPPI 9 km',  unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 9 km.' },
    CAPPI_10: { label: 'CAPPI 10 km', unit: 'dBZ', stepMs: 300_000, category: 'cappi', colorScale: 'dbz', description: 'Riflettività radar a quota costante 10 km.' },
    SITES: {
      label: 'Siti Radar',
      unit: '',
      stepMs: null,
      category: 'meta',
      colorScale: null,
      description: 'Posizione e copertura dei siti radar della rete italiana.',
    },
  },

  CATEGORIES: {
    base:     { label: 'Base',      icon: '📡' },
    cumulate: { label: 'Cumulate',  icon: '🌧️' },
    storm:    { label: 'Temporale', icon: '⛈️' },
    cappi:    { label: 'CAPPI',     icon: '🔵' },
    altro:    { label: 'Altro',     icon: '🌡️' },
    meta:     { label: 'Info',      icon: 'ℹ️' },
  },

  // ───────────────────────────────────────────────────────────────────────
  // Scale colori: ogni scala è un array di [valore_soglia, [r, g, b, a]]
  // Interpolazione lineare tra i punti.
  // ───────────────────────────────────────────────────────────────────────
  COLOR_SCALES: {
    dbz: [
      [-30, [  0,   0,   0,   0]],
      [  0, [100, 149, 237,  60]],
      [ 10, [  0, 230, 230, 140]],
      [ 20, [  0, 200,   0, 180]],
      [ 30, [255, 255,   0, 200]],
      [ 35, [255, 180,   0, 215]],
      [ 40, [255,  80,   0, 230]],
      [ 45, [255,   0,   0, 240]],
      [ 55, [150,   0, 150, 250]],
      [ 65, [255,   0, 255, 255]],
    ],
    accumulation: [
      [  0, [  0,   0,   0,   0]],
      [0.5, [180, 220, 255, 100]],
      [  1, [100, 170, 255, 150]],
      [  2, [ 50, 100, 255, 180]],
      [  5, [  0, 210, 100, 200]],
      [ 10, [  0, 210,   0, 210]],
      [ 20, [255, 240,   0, 220]],
      [ 30, [255, 170,   0, 230]],
      [ 50, [255,  60,   0, 240]],
      [ 80, [200,   0,   0, 250]],
      [120, [120,   0,  50, 255]],
    ],
    rain_rate: [
      [  0, [  0,   0,   0,   0]],
      [0.1, [180, 220, 255, 100]],
      [0.5, [100, 170, 255, 150]],
      [  1, [  0, 220, 220, 180]],
      [  2, [  0, 210,   0, 200]],
      [  5, [255, 255,   0, 215]],
      [ 10, [255, 165,   0, 230]],
      [ 20, [255,   0,   0, 245]],
      [ 50, [200,   0, 150, 255]],
    ],
    vil: [
      [ 0, [  0,   0,   0,   0]],
      [ 1, [  0, 230, 230, 150]],
      [ 5, [  0, 200,   0, 180]],
      [10, [255, 255,   0, 200]],
      [20, [255, 165,   0, 220]],
      [40, [255,   0,   0, 240]],
      [70, [150,   0, 150, 255]],
    ],
    etm: [
      [ 0, [  0,   0,   0,   0]],
      [ 2, [100, 200, 255, 150]],
      [ 5, [  0, 200,   0, 180]],
      [ 8, [255, 255,   0, 200]],
      [12, [255, 165,   0, 220]],
      [15, [255,   0,   0, 240]],
      [20, [150,   0, 150, 255]],
    ],
    probability: [
      [ 0, [  0,   0,   0,   0]],
      [10, [200, 230, 255, 120]],
      [30, [255, 255,   0, 180]],
      [50, [255, 165,   0, 210]],
      [70, [255,  60,   0, 235]],
      [90, [200,   0,   0, 255]],
    ],
    temperature: [
      [-20, [ 80,   0, 150, 220]],
      [-10, [  0,   0, 200, 220]],
      [  0, [  0, 200, 255, 220]],
      [ 10, [  0, 200,   0, 220]],
      [ 20, [255, 255,   0, 220]],
      [ 30, [255, 120,   0, 220]],
      [ 40, [255,   0,   0, 220]],
    ],
  },

  // ───────────────────────────────────────────────────────────────────────
  // Soglie allerta per prodotto (warn = avviso, danger = critico)
  // ───────────────────────────────────────────────────────────────────────
  ALERT_THRESHOLDS: {
    SRI:   { warn: 10,  danger: 30,  unit: 'mm/h'  },
    CUM3:  { warn: 20,  danger: 50,  unit: 'mm'    },
    CUM6:  { warn: 40,  danger: 80,  unit: 'mm'    },
    CUM12: { warn: 60,  danger: 120, unit: 'mm'    },
    CUM24: { warn: 80,  danger: 150, unit: 'mm'    },
    VIL:   { warn: 20,  danger: 50,  unit: 'kg/m²' },
    ETM:   { warn: 10,  danger: 15,  unit: 'km'    },
    POH:   { warn: 50,  danger: 80,  unit: '%'     },
    VMI:   { warn: 40,  danger: 55,  unit: 'dBZ'   },
  },
};
