/**
 * main.js — Orchestrazione applicazione principale
 * Collega tutti i moduli: mappa, player, location, chart, alerts.
 */

// ─── Globals ─────────────────────────────────────────────────────────────────
let map, tileLayer;
let currentProduct = 'VMI';
let autoRefreshTimer = null;
let isDarkTheme = true;
let currentGeoRaster = null;

// ─── Utility ─────────────────────────────────────────────────────────────────
function showToast(msg, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  const icons = { info: 'ℹ️', warn: '⚠️', error: '❌', success: '✅' };
  el.innerHTML = `<span class="toast-icon">${icons[type] ?? 'ℹ️'}</span><span>${msg}</span>`;
  container.appendChild(el);
  setTimeout(() => el.classList.add('show'), 10);
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, duration);
}

// ─── Init mappa ───────────────────────────────────────────────────────────────
function initMap() {
  map = L.map('map', {
    center: CONFIG.MAP.CENTER,
    zoom: CONFIG.MAP.ZOOM,
    zoomControl: false,
    attributionControl: true,
  });

  // Layer picker: 5 basemap free (CARTO Dark/Light, OSM, OSM Humanitario, Satellite Esri)
  BasemapPicker.init(map);

  // Zoom control posizionato in alto a destra
  L.control.zoom({ position: 'topright' }).addTo(map);

  // GeoRasterUtils e LocationPanel
  GeoRasterUtils.init(map);
}

// ─── Cambio prodotto ──────────────────────────────────────────────────────────
async function selectProduct(type) {
  currentProduct = type;
  const prod = CONFIG.PRODUCTS[type];
  if (!prod) return;

  // Aggiorna UI sidebar
  document.querySelectorAll('.prod-btn').forEach(b => b.classList.remove('active'));
  document.querySelector(`.prod-btn[data-type="${type}"]`)?.classList.add('active');

  document.getElementById('product-title').textContent = prod.label;
  document.getElementById('product-desc').textContent  = prod.description ?? '';

  // Legenda
  ColorMap.renderLegend(prod.colorScale, prod.unit, 'legend-container');

  ChartPanel.setProduct(type);

  // Carica il player con il prodotto
  setStatus('loading', 'Caricamento prodotto…');
  try {
    await Player.loadProduct(type);
    setStatus('ok', 'Aggiornato');
  } catch (e) {
    setStatus('error', 'Errore caricamento');
    showToast('Errore: ' + e.message, 'error');
  }

  // Aggiorna URL params
  _updateUrlParams();
}

// ─── Callback player frame ────────────────────────────────────────────────────
async function onFrameChange(timestamp, idx, total) {
  setStatus('loading', 'Rendering…');
  try {
    const prod = CONFIG.PRODUCTS[currentProduct];
    currentGeoRaster = await GeoRasterUtils.loadAndShow(
      currentProduct, timestamp, prod.colorScale,
      parseFloat(document.getElementById('opacity-slider')?.value ?? 0.8)
    );

    // Estrai valori per i punti selezionati
    const points = LocationPanel.getPoints();
    if (points.length && currentGeoRaster) {
      const extractions = LocationPanel.extractAll(currentGeoRaster);
      ChartPanel.addData(timestamp, extractions);
      AlertSystem.check(currentProduct, timestamp, extractions);
      _updateStatsPanel(extractions, prod.unit);
    }

    setStatus('ok', 'Frame ' + (idx + 1) + '/' + total);
  } catch (e) {
    setStatus('error', 'Render fallito');
    console.error('onFrameChange:', e);
  }
}

// ─── Pannello statistiche ─────────────────────────────────────────────────────
function _updateStatsPanel(extractions, unit) {
  const el = document.getElementById('stats-panel');
  if (!el) return;
  if (!extractions.length) { el.innerHTML = ''; return; }

  el.innerHTML = extractions.map(({ point, result }) => {
    const v = result.mean;
    const prod = CONFIG.PRODUCTS[currentProduct];
    const thres = CONFIG.ALERT_THRESHOLDS[currentProduct];
    let levelClass = '';
    if (thres && v !== null) {
      if (v >= thres.danger) levelClass = 'stat-danger';
      else if (v >= thres.warn) levelClass = 'stat-warn';
    }
    const colorDot = `<span class="stat-dot" style="background:${point.color}"></span>`;
    return `
      <div class="stat-item ${levelClass}">
        ${colorDot}
        <div class="stat-info">
          <span class="stat-name">${point.label}</span>
          <span class="stat-value">${v !== null ? v.toFixed(2) + ' ' + unit : 'N/D'}</span>
          ${result.count ? `<span class="stat-meta">Pixel: ${result.count} · Min: ${result.min?.toFixed(1)} · Max: ${result.max?.toFixed(1)}</span>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

// ─── Status bar ───────────────────────────────────────────────────────────────
function setStatus(type, msg) {
  const el = document.getElementById('status-bar');
  if (!el) return;
  el.className = 'status-bar status-' + type;
  el.innerHTML = {
    loading: `<i class="fa fa-circle-notch fa-spin"></i> ${msg}`,
    ok:      `<i class="fa fa-check-circle"></i> ${msg}`,
    error:   `<i class="fa fa-exclamation-triangle"></i> ${msg}`,
  }[type] || msg;
}

// ─── Auto-refresh ─────────────────────────────────────────────────────────────
function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(async () => {
    try {
      await Player.loadProduct(currentProduct);
      showToast('Dati aggiornati', 'success', 2000);
    } catch {}
  }, CONFIG.REFRESH_MS);
}

function stopAutoRefresh() {
  clearInterval(autoRefreshTimer);
  autoRefreshTimer = null;
}

// ─── URL params ───────────────────────────────────────────────────────────────
function _updateUrlParams() {
  const u = new URL(window.location);
  u.searchParams.set('product', currentProduct);
  window.history.replaceState({}, '', u);
}

function _readUrlParams() {
  const u = new URL(window.location);
  const prod = u.searchParams.get('product');
  if (prod && CONFIG.PRODUCTS[prod]) return prod;
  return 'VMI';
}

// ─── Tema chiaro/scuro ────────────────────────────────────────────────────────
function toggleTheme() {
  isDarkTheme = !isDarkTheme;
  document.body.classList.toggle('light-theme', !isDarkTheme);
  if (typeof BasemapPicker !== 'undefined') {
    BasemapPicker.applyTheme(isDarkTheme ? 'dark' : 'light');
  }
}

// ─── Build sidebar prodotti ───────────────────────────────────────────────────
function _buildProductList() {
  const container = document.getElementById('product-list');
  if (!container) return;

  const groups = {};
  Object.entries(CONFIG.PRODUCTS).forEach(([type, prod]) => {
    const cat = prod.category;
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push({ type, prod });
  });

  container.innerHTML = Object.entries(groups).map(([cat, items]) => {
    const catInfo = CONFIG.CATEGORIES[cat];
    const buttons = items.map(({ type, prod }) => `
      <button class="prod-btn" data-type="${type}" title="${prod.label}">
        <span class="prod-code">${type.replace('_', ' ')}</span>
        <span class="prod-unit">${prod.unit || '-'}</span>
      </button>
    `).join('');
    return `
      <div class="prod-category">
        <div class="cat-header">${catInfo?.icon ?? ''} ${catInfo?.label ?? cat}</div>
        <div class="cat-items">${buttons}</div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('.prod-btn').forEach(btn => {
    btn.addEventListener('click', () => selectProduct(btn.dataset.type));
  });
}

// ─── WebSocket push integration ───────────────────────────────────────────────
let _wssUnsub = null;

function initWebSocket() {
  RadarWebSocket.connect((status) => {
    // Aggiorna l'icona auto-refresh quando il WSS è connesso
    const ar = document.getElementById('auto-refresh');
    if (status === 'connected' && ar) {
      ar.checked = false; // il WSS sostituisce il polling
      stopAutoRefresh();
    }
  });

  // Handler globale: aggiorna il layer solo se il prodotto corrente è arrivato
  _wssUnsub = RadarWebSocket.on(null, async (msg) => {
    if (msg.productType !== currentProduct) return;
    // Il WebSocket ci dà il timestamp esatto → ricarica solo l'ultimo frame
    try {
      setStatus('loading', `⚡ Nuovo ${msg.productType} ricevuto…`);
      const prod = CONFIG.PRODUCTS[currentProduct];
      currentGeoRaster = await GeoRasterUtils.loadAndShow(
        currentProduct, msg.time, prod.colorScale, _opacity()
      );
      // Aggiorna le estrazioni per i punti selezionati
      const points = LocationPanel.getPoints();
      if (points.length && currentGeoRaster) {
        const extractions = LocationPanel.extractAll(currentGeoRaster);
        ChartPanel.addData(msg.time, extractions);
        AlertSystem.check(currentProduct, msg.time, extractions);
        _updateStatsPanel(extractions, prod.unit);
      }
      // Aggiorna il player con il nuovo timestamp
      await Player.loadProduct(currentProduct, CONFIG.MAX_FRAMES);
      setStatus('ok', `⚡ Live — ${_formatTs(msg.time)}`);
      showToast(`Nuovo ${msg.productType} disponibile`, 'info', 2500);
    } catch (e) {
      setStatus('error', 'Errore aggiornamento WSS');
    }
  });
}

function _opacity() {
  return parseFloat(document.getElementById('opacity-slider')?.value ?? 0.8);
}

function _formatTs(ms) {
  return Timezone.formatTime(ms);
}

// ─── Bootstrap ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initMap();
  _buildProductList();
  _bindTimezoneToggle();
  // Inizializza tab Archivio (lazy: legge i CSV solo all'apertura prima volta)
  if (typeof ArchiveTab !== 'undefined') {
    ArchiveTab.init().catch(e => console.warn('[archive] init err:', e));
  }

  Player.init({
    onFrameChange,
    onLoadProgress: (loaded, total) => {
      const el = document.getElementById('preload-bar');
      if (!el) return;
      el.style.width = total > 0 ? `${Math.round(loaded / total * 100)}%` : '0%';
      el.parentElement.style.display = loaded < total ? '' : 'none';
    },
  });

  LocationPanel.init(map, (points) => {
    if (!points.length) {
      document.getElementById('stats-panel').innerHTML = '';
    }
  });

  ChartPanel.init();
  AlertSystem.init();

  // Opacity slider
  document.getElementById('opacity-slider')?.addEventListener('input', (e) => {
    GeoRasterUtils.setOpacity(parseFloat(e.target.value));
    document.getElementById('opacity-val').textContent = Math.round(e.target.value * 100) + '%';
  });

  // Auto-refresh toggle (polling fallback quando WSS non è connesso)
  document.getElementById('auto-refresh')?.addEventListener('change', (e) => {
    if (e.target.checked) startAutoRefresh(); else stopAutoRefresh();
  });

  // Tema
  document.getElementById('btn-theme')?.addEventListener('click', toggleTheme);

  // Screenshot mappa
  document.getElementById('btn-screenshot')?.addEventListener('click', async () => {
    showToast('Usa il tasto Stampa schermo del browser (F12 → Screenshot)', 'info');
  });

  // Download CSV/Excel dalla tab chart
  document.getElementById('btn-download-chart-csv')?.addEventListener('click', ChartPanel.downloadCSV);
  document.getElementById('btn-download-chart-xlsx')?.addEventListener('click', ChartPanel.downloadExcel);

  // Pannelli tab
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      if (!target) return;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-' + target)?.classList.add('active');
    });
  });

  // Carica prodotto iniziale
  const startProduct = _readUrlParams();
  await selectProduct(startProduct);

  // Avvia WebSocket
  initWebSocket();

  // Avvia auto-refresh polling SOLO se WSS non si connette entro 5s (fallback)
  setTimeout(() => {
    if (!RadarWebSocket.isConnected() && document.getElementById('auto-refresh')?.checked) {
      startAutoRefresh();
      showToast('WSS non disponibile — uso polling REST', 'warn', 5000);
    }
  }, 5000);
});


// ─── Toggle UTC ↔ Locale (richiamato dal bottone in topbar) ───────────────────
function _bindTimezoneToggle() {
  const btn = document.getElementById('btn-timezone');
  if (!btn) return;

  function _refreshLabel() {
    const lbl = btn.querySelector('.tz-label');
    if (lbl) lbl.textContent = Timezone.mode === 'utc' ? 'UTC' : 'Locale';
    btn.title = Timezone.mode === 'utc'
      ? 'Mostra ora locale (Europe/Rome)'
      : 'Mostra ora UTC (come API DPC)';
  }
  _refreshLabel();

  btn.addEventListener('click', () => Timezone.toggle());

  window.addEventListener('timezone-changed', () => {
    _refreshLabel();
    // Aggiorna l'orologio del frame attivo
    const cur = document.getElementById('current-time');
    if (cur && Player && typeof Player.getCurrentTimestamp === 'function') {
      const ts = Player.getCurrentTimestamp();
      if (ts) cur.textContent = _formatTs(ts);
    }
  });
}
