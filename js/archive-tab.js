/**
 * archive-tab.js — Tab "Archivio" dell'app principale
 *
 * Mostra i dati storici raccolti automaticamente dallo script
 * archive/scripts/collect.py per le aree configurate.
 *
 * Funzionalità:
 *   - selettore area (Ruspino / Scarperia / Cepina)
 *   - statistiche aggregate (totale, max, giorni con pioggia, ecc.)
 *   - mini-mappa con poligono dell'area + arealizzazione IDW dei 5 vertici
 *     campione (animata sugli 8 timestamp CUM3 del giorno selezionato)
 *   - grafico time series CUM24 (Chart.js)
 *   - grafico time series CUM3 (Chart.js)
 *   - selettore range date
 *
 * I dati vengono letti dai CSV in archive/data/. Sono accessibili nello
 * stesso origin (https://<user>.github.io/radar-dpc/) quindi niente CORS.
 */

const ArchiveTab = (() => {

  // ─── Configurazione ───────────────────────────────────────────────────────
// Dati sempre freschi da raw@main (deploy Pages non più triggerato dai push dati).
const RAW_BASE = 'https://raw.githubusercontent.com/EbbeneTriglav/radar-dpc/main/';
async function fetchData(path){
  if (location.hostname.endsWith('github.io')) {
    try {
      const r = await fetch(RAW_BASE + path + '?_=' + Date.now(), { cache: 'no-cache' });
      if (r.ok) return r;
    } catch {}
  }
  return fetch(path + '?_=' + Date.now(), { cache: 'no-cache' });
}
  const AREAS_URL    = 'archive/areas.json';
  const DATA_BASE    = 'archive/data';
  const IDW_POWER    = 2;     // esponente IDW: 2 dà transizioni morbide
  const IDW_PIXEL_PX = 4;     // dimensione "pixel" canvas in pixel CSS
  const SLOT_MS = 3 * 3600_000;    // blocchi CUM3 a ore fisse UTC (00,03,…,21), timestamp = fine blocco
  const DAY_MS  = 86400_000;
  // Scala colori FISSA per la mappa IDW (mm/3h) — scelta di visualizzazione, non un dato.
  // Classi: [da, a) mm/3h; sotto 0.1 mm trasparente.
  const IDW_BINS = [
    { from: 0.1, to: 1,    rgba: [100, 170, 255, 110] },
    { from: 1,   to: 2.5,  rgba: [  0, 220, 220, 160] },
    { from: 2.5, to: 5,    rgba: [  0, 200,   0, 200] },
    { from: 5,   to: 10,   rgba: [255, 200,   0, 220] },
    { from: 10,  to: 20,   rgba: [255, 120,   0, 230] },
    { from: 20,  to: null, rgba: [230,  30,  60, 240] },
  ];

  // Colori grafici leggibili in tema scuro e chiaro
  function _isLight() { return document.body.classList.contains('light-theme'); }
  function _cssVar(name, fallback) {
    try { return getComputedStyle(document.body).getPropertyValue(name).trim() || fallback; }
    catch { return fallback; }
  }
  function _chartColors() {
    return _isLight()
      ? { cum24: '#1e9e57', cum3: '#1f6fd1', fc: '#7a3fd1' }
      : { cum24: '#7bed9f', cum3: '#3eaaff', fc: '#c3a6ff' };
  }

  let _areasConfig = null;
  let _currentArea = null;        // nome area selezionata
  let _data = {};                 // { ruspino: { cum24: [...], cum3: [...] }, ... }
  let _chartCum24 = null;
  let _chartCum3  = null;
  let _miniMap = null;
  let _miniPolyLayer = null;
  let _miniIdwCanvas = null;       // L.canvas overlay
  let _animationTimer = null;

  let _selectedDateMs = null;      // giorno selezionato per l'animazione CUM3
  let _cum3RenderSeq = 0;          // evita che un render vecchio (fetch lento) sovrascriva quello nuovo
  const _omCache = {};             // { area: { t, blocks: Map(endMs → mm|null) } }

  // ─── Init ─────────────────────────────────────────────────────────────────
  async function init() {
    const panel = document.getElementById('tab-archive');
    if (!panel) return;

    // Carica config aree
    try {
      const r = await fetchData(AREAS_URL);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _areasConfig = await r.json();
    } catch (e) {
      panel.innerHTML = `<div style="padding:20px;color:var(--text3)">
        <p><i class="fa fa-info-circle"></i> Archivio non ancora disponibile.</p>
        <p style="font-size:11px">Lancia il workflow <code>archive-daily.yml</code>
        da GitHub Actions (Run workflow → days=7) per popolare i dati iniziali.</p>
      </div>`;
      return;
    }

    _renderShell();
    _bindEvents();

    // Seleziona prima area di default
    if (_areasConfig.areas?.length) {
      await selectArea(_areasConfig.areas[0].name);
    }
  }

  // ─── Costruzione struttura UI ─────────────────────────────────────────────
  function _renderShell() {
    const panel = document.getElementById('tab-archive');
    if (!panel) return;

    const areaButtons = _areasConfig.areas.map((a, i) => `
      <button class="archive-area-btn${i === 0 ? ' active' : ''}" data-area="${a.name}">
        📍 ${a.label}
      </button>
    `).join('');

    panel.innerHTML = `
      <div class="archive-toolbar">
        <div class="archive-area-picker">${areaButtons}</div>
        <div class="archive-info" id="archive-info"></div>
      </div>
      <div class="archive-body">
        <div class="archive-col-map">
          <div id="archive-map" class="archive-mini-map"></div>
          <div class="archive-anim-bar">
            <button class="player-btn" id="archive-play"><i class="fa fa-play"></i></button>
            <input type="range" id="archive-anim-slider" min="0" max="7" value="0" disabled>
            <span class="mono" id="archive-anim-label">—</span>
          </div>
          <div class="archive-map-note">
            <div id="archive-map-day">—</div>
            <div>Mappa: <b>interpolazione IDW (stima)</b> da 5 vertici + media area — CUM3 pluviometri DPC interpolati, non radar.</div>
            <div class="archive-legend">${_legendHtml()}</div>
          </div>
        </div>
        <div class="archive-col-charts">
          <div class="archive-chart-wrap">
            <div class="archive-chart-title">
              CUM24 — pioggia del giorno UTC (00→24), pluviometri DPC interpolati <span id="archive-cum24-summary"></span>
            </div>
            <canvas id="archive-chart-cum24"></canvas>
          </div>
          <div class="archive-chart-wrap">
            <div class="archive-chart-title">
              CUM3 — blocchi 3h a ore fisse UTC (8 attesi/giorno), pluviometri DPC interpolati <span id="archive-cum3-summary"></span>
            </div>
            <canvas id="archive-chart-cum3"></canvas>
          </div>
        </div>
      </div>
    `;

    // CSS inline per evitare modifiche a style.css
    if (!document.getElementById('archive-tab-style')) {
      const s = document.createElement('style');
      s.id = 'archive-tab-style';
      s.textContent = `
        #tab-archive { display:flex; flex-direction:column; height:100%; overflow:hidden; }
        .archive-toolbar { display:flex; gap:12px; align-items:center; padding:6px 10px;
          border-bottom:1px solid var(--border2); flex-shrink:0; }
        .archive-area-picker { display:flex; gap:6px; }
        .archive-area-btn { background:var(--bg2); color:var(--text2); border:1px solid var(--border2);
          padding:4px 10px; border-radius:14px; font-size:11px; cursor:pointer; }
        .archive-area-btn:hover { background:var(--bg3); }
        .archive-area-btn.active { background:#1e3a5f; color:#cfe7ff; border-color:#3a6ea5; }
        .archive-info { font-size:10px; color:var(--text3); flex:1; }
        .archive-body { display:flex; flex:1; gap:8px; padding:8px; overflow:hidden; }
        .archive-col-map { display:flex; flex-direction:column; width:38%; min-width:280px; gap:6px; }
        .archive-mini-map { flex:1; min-height:220px; border:1px solid var(--border2); border-radius:4px;
          background:var(--bg2); }
        .archive-anim-bar { display:flex; gap:8px; align-items:center; padding:4px 8px;
          background:var(--bg2); border:1px solid var(--border2); border-radius:4px; }
        .archive-anim-bar input[type=range] { flex:1; }
        .archive-anim-bar .mono { font-size:11px; color:var(--text2); min-width:110px; text-align:right; }
        .archive-col-charts { flex:1; display:flex; flex-direction:column; gap:6px; overflow:hidden; }
        .archive-chart-wrap { flex:1; display:flex; flex-direction:column; min-height:0;
          background:var(--bg2); border:1px solid var(--border2); border-radius:4px; padding:6px; }
        .archive-chart-title { font-size:11px; color:var(--text2); margin-bottom:4px; flex-shrink:0; }
        .archive-chart-title span { color:var(--text3); font-size:10px; margin-left:6px; }
        .archive-chart-wrap canvas { flex:1; min-height:0; }
        .archive-map-note { font-size:10px; color:var(--text3); line-height:1.5; padding:2px 4px; }
        .archive-map-note b { color:var(--text2); font-weight:600; }
        #archive-map-day { color:var(--text2); }
        .archive-legend { display:flex; flex-wrap:wrap; gap:4px 8px; align-items:center; margin-top:2px; }
        .archive-legend i { display:inline-block; width:12px; height:10px; border-radius:2px; vertical-align:middle; margin-right:3px; }
        .archive-toolbar { flex-wrap:wrap; }
        .archive-area-picker { flex-wrap:wrap; }
        .archive-warn, .archive-chart-title .archive-warn { color:var(--warn); }
        @media (max-width:700px) {
          .archive-body { flex-direction:column; overflow:auto; }
          .archive-col-map { width:100%; min-width:0; flex-shrink:0; }
          .archive-mini-map { min-height:300px !important; flex:none; height:300px; }
          .archive-col-charts { overflow:visible; flex:none; }
          .archive-chart-wrap { flex:none; height:280px; }
        }
      `;
      document.head.appendChild(s);
    }
  }

  function _bindEvents() {
    document.querySelectorAll('.archive-area-btn').forEach(btn => {
      btn.addEventListener('click', () => selectArea(btn.dataset.area));
    });
    document.getElementById('archive-play')?.addEventListener('click', _toggleAnim);
    document.getElementById('archive-anim-slider')?.addEventListener('input', e => {
      _stopAnim();
      _showFrame(parseInt(e.target.value, 10));
    });
    // Re-render al cambio TZ (timestamps in grafico cambiano)
    window.addEventListener('timezone-changed', () => {
      if (_currentArea) _renderCharts();
    });
    // Re-render al cambio tema (colori assi/legenda letti dalle variabili CSS)
    window.addEventListener('radar-theme-changed', () => {
      if (_currentArea) _renderCharts();
    });
  }

  // ─── Caricamento CSV per area ─────────────────────────────────────────────
  async function selectArea(name) {
    _currentArea = name;
    document.querySelectorAll('.archive-area-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.area === name);
    });

    if (!_data[name]) {
      _data[name] = { cum24: null, cum3: null };
      try {
        _data[name].cum24 = await _loadCsv(`${DATA_BASE}/${name}_cum24.csv`);
        _data[name].cum3  = await _loadCsv(`${DATA_BASE}/${name}_cum3.csv`);
      } catch (e) {
        console.warn('[archive] load fallito per', name, e);
      }
    }

    _renderInfo();
    _renderMiniMap();
    _renderCharts();
    _setupAnimation();
  }

  async function _loadCsv(url) {
    const r = await fetchData(url);
    if (!r.ok) {
      if (r.status === 404) return [];
      throw new Error(`HTTP ${r.status}`);
    }
    const text = await r.text();
    return _parseCsv(text);
  }

  function _parseCsv(text) {
    const lines = text.trim().split(/\r?\n/);
    if (lines.length < 2) return [];
    const headers = lines[0].split(',');
    return lines.slice(1).map(line => {
      const cells = line.split(',');
      const row = {};
      headers.forEach((h, i) => { row[h] = cells[i] ?? ''; });
      return row;
    });
  }

  // ─── Info riepilogative ──────────────────────────────────────────────────
  function _renderInfo() {
    const info = document.getElementById('archive-info');
    if (!info) return;
    const cum24 = _data[_currentArea]?.cum24 || [];
    const cum3  = _data[_currentArea]?.cum3  || [];
    const areaRows24 = cum24.filter(r => r.location_type === 'area');
    const areaRows3  = cum3.filter(r  => r.location_type === 'area');
    if (!areaRows24.length && !areaRows3.length) {
      info.innerHTML = '<i class="fa fa-info-circle"></i> ' +
        'Archivio vuoto per questa area. ' +
        'Lancia il workflow <code>archive-daily.yml</code> da GitHub Actions ' +
        '(Run workflow → days=7) per il bootstrap iniziale.';
      return;
    }
    // CUM24 con timestamp T = 24h che TERMINANO a T → il giorno coperto è T − 1 giorno
    const dates24 = areaRows24.map(r => _cum24Day(r.timestamp_utc)).sort();
    const g3 = _cum3Gaps(areaRows3);
    // Ultimo aggiornamento = fetched_at_utc più recente nei CSV caricati
    const lastFetch = [...cum24, ...cum3].reduce(
      (m, r) => (r.fetched_at_utc && r.fetched_at_utc > m ? r.fetched_at_utc : m), '');
    const lastStr = lastFetch
      ? ((typeof Timezone !== 'undefined') ? Timezone.formatDateTime(Date.parse(lastFetch)) : lastFetch)
      : '—';
    info.innerHTML = `CUM24: ${areaRows24.length} giorni${dates24.length ? ` (${dates24[0]}…${dates24[dates24.length-1]}, giorni UTC)` : ''}` +
                     ` • CUM3: ${areaRows3.length} blocchi` +
                     (g3.missing ? ` <span class="archive-warn">(${g3.missing} mancanti)</span>` : '') +
                     ` • Ultimo aggiornamento dati: ${lastStr}`;
  }

  /** Giorno UTC (YYYY-MM-DD) coperto da una riga CUM24 con timestamp di fine finestra. */
  function _cum24Day(tsIso) {
    return new Date(Date.parse(tsIso) - DAY_MS).toISOString().slice(0, 10);
  }

  /** Blocchi CUM3 attesi tra il primo e l'ultimo timestamp e quanti mancano (righe area). */
  function _cum3Gaps(areaRows3) {
    const have = new Set();
    areaRows3.forEach(r => {
      const ms = Date.parse(r.timestamp_utc);
      if (isFinite(ms) && r.mean !== '' && r.mean != null) have.add(ms);
    });
    if (!have.size) return { expected: 0, missing: 0 };
    const all = [...have];
    const t0 = Math.min(...all), t1 = Math.max(...all);
    const expected = Math.round((t1 - t0) / SLOT_MS) + 1;
    return { expected, missing: Math.max(0, expected - have.size) };
  }

  // ─── Mini-mappa con poligono e arealizzazione IDW ────────────────────────
  function _renderMiniMap() {
    if (!_currentArea) return;
    const area = _areasConfig.areas.find(a => a.name === _currentArea);
    if (!area) return;

    if (!_miniMap) {
      _miniMap = L.map('archive-map', { zoomControl: true, attributionControl: false });
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
        attribution: '© Esri', maxZoom: 16,
      }).addTo(_miniMap);
    } else {
      if (_miniPolyLayer) _miniMap.removeLayer(_miniPolyLayer);
      if (_miniIdwCanvas) _miniMap.removeLayer(_miniIdwCanvas);
    }

    const latLngs = area.polygon.map(([la, lo]) => [la, lo]);
    _miniPolyLayer = L.polygon(latLngs, {
      color: '#7bed9f', weight: 2, fillOpacity: 0,
    }).addTo(_miniMap);
    _miniMap.fitBounds(_miniPolyLayer.getBounds(), { padding: [20, 20] });

    // Vertici come marker
    area.sample_vertices.forEach(v => {
      L.circleMarker([v.lat, v.lon], {
        radius: 4, color: '#7bed9f', fillColor: '#7bed9f', fillOpacity: 0.8, weight: 1,
      }).addTo(_miniMap).bindTooltip(v.id, { permanent: false });
    });
  }

  // ─── Animazione frame CUM3 per il giorno più recente ──────────────────────
  function _setupAnimation() {
    const slider = document.getElementById('archive-anim-slider');
    const label  = document.getElementById('archive-anim-label');
    if (!slider || !label) return;

    const cum3 = _data[_currentArea]?.cum3 || [];
    const days = _availableDays(cum3);
    if (!days.length) {
      slider.disabled = true;
      slider.value = 0;
      slider.max = 7;
      label.textContent = 'no data';
      _clearIdwOverlay();
      return;
    }

    // Giorno di default: l'ultimo con tutti gli 8 blocchi CUM3 (giorno concluso e completo),
    // non il giorno in corso (parziale). Se nessun giorno è completo: l'ultimo con dati.
    const isComplete = d => {
      const f = _framesForDay(cum3, d);
      return f.length === 8 && f.every(x => x.area && x.area.mean !== '');
    };
    let pick = null;
    for (let i = days.length - 1; i >= 0 && pick == null; i--) if (isComplete(days[i])) pick = days[i];
    let dayNote = 'giorno completo';
    if (pick == null) {
      for (let i = days.length - 1; i >= 0 && pick == null; i--) {
        if (_framesForDay(cum3, days[i]).length) pick = days[i];
      }
      dayNote = 'nessun giorno completo: blocchi parziali';
    }
    _selectedDateMs = pick ?? days[days.length - 1];
    const frames = _framesForDay(cum3, _selectedDateMs);
    const dayEl = document.getElementById('archive-map-day');
    if (dayEl) {
      dayEl.textContent = `Giorno animato: ${new Date(_selectedDateMs).toISOString().slice(0, 10)} UTC — ` +
        `${frames.length}/8 blocchi, ${dayNote}`;
    }
    slider.disabled = frames.length === 0;
    slider.max = Math.max(0, frames.length - 1);
    slider.value = 0;
    _showFrame(0);
  }

  function _availableDays(cum3rows) {
    const set = new Set();
    cum3rows.forEach(r => {
      if (r.location_type !== 'area') return;
      set.add(r.timestamp_utc.slice(0, 10));
    });
    return [...set].sort().map(d => new Date(d + 'T00:00:00Z').getTime());
  }

  function _framesForDay(cum3rows, dayMs) {
    const dayStr = new Date(dayMs).toISOString().slice(0, 10);
    // Per CUM3 il giorno X copre 03:00...21:00 del giorno X + 00:00 del giorno X+1
    const nextDay = new Date(dayMs + 86400000).toISOString().slice(0, 10);

    const byTs = {};
    cum3rows.forEach(r => {
      const ts = r.timestamp_utc;
      const tsDay = ts.slice(0, 10);
      const tsHour = ts.slice(11, 13);
      let belongs = false;
      if (tsDay === dayStr && tsHour !== '00') belongs = true;
      else if (tsDay === nextDay && tsHour === '00') belongs = true;
      if (!belongs) return;
      if (!byTs[ts]) byTs[ts] = { ts, area: null, vertices: [] };
      if (r.location_type === 'area')  byTs[ts].area = r;
      if (r.location_type === 'vertex') byTs[ts].vertices.push(r);
    });

    return Object.values(byTs).sort((a, b) => a.ts.localeCompare(b.ts));
  }

  function _showFrame(idx) {
    const cum3 = _data[_currentArea]?.cum3 || [];
    const frames = _framesForDay(cum3, _selectedDateMs);
    if (!frames.length) return;
    idx = Math.max(0, Math.min(idx, frames.length - 1));
    const frame = frames[idx];

    const slider = document.getElementById('archive-anim-slider');
    const label  = document.getElementById('archive-anim-label');
    if (slider) slider.value = idx;
    if (label) {
      const tsStr = (typeof Timezone !== 'undefined')
        ? Timezone.formatDateTime(new Date(frame.ts).getTime())
        : frame.ts;
      const mean = (frame.area && frame.area.mean !== '') ? parseFloat(frame.area.mean).toFixed(1) : '—';
      label.textContent = `${tsStr} • media area ${mean} mm/3h`;
    }

    _drawIdwOverlay(frame);
  }

  function _toggleAnim() {
    if (_animationTimer) { _stopAnim(); return; }
    const btn = document.getElementById('archive-play');
    if (btn) btn.innerHTML = '<i class="fa fa-pause"></i>';
    const cum3 = _data[_currentArea]?.cum3 || [];
    const frames = _framesForDay(cum3, _selectedDateMs);
    if (!frames.length) return;
    let i = parseInt(document.getElementById('archive-anim-slider').value, 10) || 0;
    _animationTimer = setInterval(() => {
      i = (i + 1) % frames.length;
      _showFrame(i);
    }, 1000);
  }

  function _stopAnim() {
    if (_animationTimer) { clearInterval(_animationTimer); _animationTimer = null; }
    const btn = document.getElementById('archive-play');
    if (btn) btn.innerHTML = '<i class="fa fa-play"></i>';
  }

  // ─── Overlay IDW (canvas) ─────────────────────────────────────────────────
  function _clearIdwOverlay() {
    if (_miniIdwCanvas) {
      _miniMap.removeLayer(_miniIdwCanvas);
      _miniIdwCanvas = null;
    }
  }

  function _drawIdwOverlay(frame) {
    _clearIdwOverlay();
    const area = _areasConfig.areas.find(a => a.name === _currentArea);
    if (!area || !frame) return;

    // Punti: 5 vertici + centroide con valore = media area
    const pts = frame.vertices
      .filter(v => v.value !== '' && v.value !== null)
      .map(v => ({
        lat: parseFloat(v.lat),
        lon: parseFloat(v.lon),
        value: parseFloat(v.value),
      }));
    if (frame.area && frame.area.mean !== '') {
      pts.push({
        lat: area.centroid.lat,
        lon: area.centroid.lon,
        value: parseFloat(frame.area.mean),
      });
    }
    if (pts.length < 2) return;

    // BBox del poligono
    const lats = area.polygon.map(p => p[0]);
    const lons = area.polygon.map(p => p[1]);
    const bbox = {
      south: Math.min(...lats), north: Math.max(...lats),
      west:  Math.min(...lons), east:  Math.max(...lons),
    };

    // Scala colori FISSA (IDW_BINS, mm/3h): confrontabile tra frame e giorni.

    // Crea un canvas overlay sulla bbox
    const imgBounds = [[bbox.south, bbox.west], [bbox.north, bbox.east]];

    // Risoluzione canvas: 60×60 ~ 3600 pixel (veloce)
    const W = 60, H = 60;
    const canvas = document.createElement('canvas');
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d');
    const imgData = ctx.createImageData(W, H);

    // Pre-converti i punti in coordinate normalizzate [0..1]
    const ptsNorm = pts.map(p => ({
      x: (p.lon - bbox.west)  / (bbox.east  - bbox.west),
      y: (bbox.north - p.lat) / (bbox.north - bbox.south),  // y invertito (top→bottom)
      value: p.value,
    }));

    // Pre-calcola la maschera poligonale in canvas coords
    const polyCanvas = area.polygon.map(([la, lo]) => ({
      x: ((lo - bbox.west) / (bbox.east - bbox.west)) * W,
      y: ((bbox.north - la) / (bbox.north - bbox.south)) * H,
    }));

    for (let py = 0; py < H; py++) {
      for (let px = 0; px < W; px++) {
        if (!_pointInPoly(px + 0.5, py + 0.5, polyCanvas)) {
          // pixel fuori poligono: completamente trasparente
          const idx4 = (py * W + px) * 4;
          imgData.data[idx4 + 3] = 0;
          continue;
        }
        const nx = (px + 0.5) / W;
        const ny = (py + 0.5) / H;
        const v = _idw(nx, ny, ptsNorm);
        const [r, g, b, a] = _valueToRGBA(v);
        const idx4 = (py * W + px) * 4;
        imgData.data[idx4]     = r;
        imgData.data[idx4 + 1] = g;
        imgData.data[idx4 + 2] = b;
        imgData.data[idx4 + 3] = a;
      }
    }
    ctx.putImageData(imgData, 0, 0);

    _miniIdwCanvas = L.imageOverlay(canvas.toDataURL(), imgBounds, { opacity: 0.7 }).addTo(_miniMap);
    // Riporta sopra il poligono per evidenziarne il bordo
    if (_miniPolyLayer) _miniPolyLayer.bringToFront();
  }

  function _idw(x, y, pts) {
    let num = 0, den = 0;
    for (const p of pts) {
      const dx = x - p.x;
      const dy = y - p.y;
      const d2 = dx*dx + dy*dy;
      if (d2 < 1e-8) return p.value;
      const w = 1 / Math.pow(Math.sqrt(d2), IDW_POWER);
      num += w * p.value;
      den += w;
    }
    return num / den;
  }

  function _pointInPoly(x, y, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i].x, yi = poly[i].y;
      const xj = poly[j].x, yj = poly[j].y;
      const intersect = ((yi > y) !== (yj > y)) &&
        (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  /** Scala colori a classi FISSE in mm/3h (IDW_BINS). Sotto 0.1 mm: trasparente. */
  function _valueToRGBA(v) {
    if (!isFinite(v) || v < IDW_BINS[0].from) return [0, 0, 0, 0];
    for (const b of IDW_BINS) {
      if (b.to == null || v < b.to) return b.rgba;
    }
    return IDW_BINS[IDW_BINS.length - 1].rgba;
  }

  function _legendHtml() {
    return '<span>mm/3h (scala fissa):</span>' + IDW_BINS.map(b => {
      const [r, g, bl, a] = b.rgba;
      const lbl = b.to == null ? `≥${b.from}` : `${b.from}–${b.to}`;
      return `<span><i style="background:rgba(${r},${g},${bl},${(a / 255).toFixed(2)})"></i>${lbl}</span>`;
    }).join('');
  }

  // ─── Charts ───────────────────────────────────────────────────────────────
  function _renderCharts() {
    _renderChartCum24();
    _renderChartCum3();
  }

  function _renderChartCum24() {
    const ctx = document.getElementById('archive-chart-cum24');
    if (!ctx) return;
    const C = _chartColors();
    const rows = (_data[_currentArea]?.cum24 || []).filter(r => r.location_type === 'area');
    // Asse continuo a passo 1 giorno: i giorni senza riga restano null (buco visibile, mai 0)
    const byTs = new Map();
    rows.forEach(r => {
      const ms = Date.parse(r.timestamp_utc);
      if (!isFinite(ms)) return;
      const v = (r.mean !== '' && r.mean != null) ? parseFloat(r.mean) : null;
      byTs.set(ms, isFinite(v) ? v : null);
    });
    const tsList = [...byTs.keys()].sort((a, b) => a - b);
    const labels = [], data = [];
    let missing = 0;
    if (tsList.length) {
      for (let t = tsList[0]; t <= tsList[tsList.length - 1]; t += DAY_MS) {
        // Riga con timestamp T = 24h che terminano a T → etichetta = giorno UTC precedente
        const d = new Date(t - DAY_MS).toISOString();
        labels.push(`${d.slice(8, 10)}/${d.slice(5, 7)}`);
        const v = byTs.has(t) ? byTs.get(t) : null;
        if (v == null) missing++;
        data.push(v);
      }
    }

    const valid = data.filter(v => v != null);
    const sum   = valid.reduce((a, v) => a + v, 0);
    const max   = valid.reduce((a, v) => Math.max(a, v), -Infinity);
    const ndays = valid.filter(v => v > 0.1).length;
    const sumEl = document.getElementById('archive-cum24-summary');
    if (sumEl) {
      sumEl.innerHTML = valid.length
        ? `totale ${sum.toFixed(1)} mm • max ${max.toFixed(1)} mm • ${ndays} giorni con pioggia (&gt;0.1 mm)` +
          (missing ? ` • <span class="archive-warn">${missing} ${missing === 1 ? 'giorno mancante' : 'giorni mancanti'} — totale incompleto</span>` : '')
        : 'nessun dato (—)';
    }

    if (_chartCum24) _chartCum24.destroy();
    _chartCum24 = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'CUM24 medio area (mm, giorno UTC)',
          data,
          backgroundColor: C.cum24 + 'cc',
          borderColor: C.cum24,
          borderWidth: 1,
        }],
      },
      options: _commonChartOpts('mm/giorno'),
    });
  }

  /**
   * Previsione Open-Meteo (minutely_15, prossime 24h) aggregata in blocchi 3h UTC
   * allineati alla CUM3 DPC (timestamp = fine blocco). Il valore 15' al tempo t copre (t−15', t].
   * Un blocco senza tutti i 12 valori (es. blocco in corso, già iniziato) → null, mai 0.
   */
  async function _fetchOmBlocks(area) {
    const c = _omCache[area.name];
    if (c && Date.now() - c.t < 10 * 60_000) return c.blocks;
    const r = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${area.centroid.lat}` +
      `&longitude=${area.centroid.lon}&minutely_15=precipitation&forecast_minutes=1440&timezone=UTC`,
      { cache: 'no-cache' });
    if (!r.ok) throw new Error(`Open-Meteo HTTP ${r.status}`);
    const d = await r.json();
    const ts = d.minutely_15?.time || [];
    const pr = d.minutely_15?.precipitation || [];
    const acc = new Map();
    ts.forEach((t, i) => {
      const ms = Date.parse(t + 'Z');
      if (!isFinite(ms)) return;
      const end = Math.ceil(ms / SLOT_MS) * SLOT_MS;
      const a = acc.get(end) || { sum: 0, n: 0, bad: false };
      const v = pr[i];
      if (v == null || !isFinite(v)) a.bad = true; else { a.sum += v; a.n++; }
      acc.set(end, a);
    });
    const blocks = new Map();
    for (const [end, a] of acc) blocks.set(end, (!a.bad && a.n === 12) ? +a.sum.toFixed(2) : null);
    _omCache[area.name] = { t: Date.now(), blocks };
    return blocks;
  }

  async function _renderChartCum3() {
    const ctx = document.getElementById('archive-chart-cum3');
    if (!ctx) return;
    const seq = ++_cum3RenderSeq;
    const areaName = _currentArea;
    const rows = (_data[areaName]?.cum3 || []).filter(r => r.location_type === 'area');

    // Osservato su slot 3h attesi: blocchi assenti → null (buco visibile nel grafico)
    const obs = new Map();
    rows.forEach(r => {
      const ms = Date.parse(r.timestamp_utc);
      if (!isFinite(ms)) return;
      const v = (r.mean !== '' && r.mean != null) ? parseFloat(r.mean) : null;
      obs.set(ms, isFinite(v) ? v : null);
    });
    const obsTs = [...obs.keys()].filter(t => obs.get(t) != null).sort((a, b) => a - b);
    const g = _cum3Gaps(rows);

    const valid = obsTs.map(t => obs.get(t));
    const sum = valid.reduce((a, v) => a + v, 0);
    const max = valid.reduce((a, v) => Math.max(a, v), -Infinity);
    const sumEl = document.getElementById('archive-cum3-summary');
    if (sumEl) {
      sumEl.innerHTML = valid.length
        ? `${valid.length}/${g.expected} blocchi • totale ${sum.toFixed(1)} mm • picco 3h ${max.toFixed(1)} mm` +
          (g.missing ? ` • <span class="archive-warn">${g.missing} ${g.missing === 1 ? 'blocco mancante' : 'blocchi mancanti'} — totale incompleto</span>` : '')
        : 'nessun dato (—)';
    }

    // Previsione OpenMeteo (prossime 24h) come dataset separato sullo stesso asse 3h
    let fc = new Map();
    let fcErr = '';
    try {
      const area = _areasConfig.areas.find(a => a.name === areaName);
      if (area) fc = await _fetchOmBlocks(area);
    } catch (e) { fcErr = e.message; console.warn('[archive] OpenMeteo non disponibile:', e.message); }
    if (seq !== _cum3RenderSeq) return;   // nel frattempo è partito un render più recente

    const fcTs = [...fc.keys()].filter(t => fc.get(t) != null).sort((a, b) => a - b);
    const t0 = obsTs.length ? obsTs[0] : (fcTs[0] ?? null);
    const tEnd = Math.max(obsTs.length ? obsTs[obsTs.length - 1] : -Infinity,
                          fcTs.length ? fcTs[fcTs.length - 1] : -Infinity);
    const labels = [], observed = [], forecast = [];
    if (t0 != null && isFinite(tEnd)) {
      for (let t = t0; t <= tEnd; t += SLOT_MS) {
        labels.push((typeof Timezone !== 'undefined')
          ? Timezone.format(t, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
          : new Date(t).toISOString().slice(0, 16).replace('T', ' '));
        observed.push(obs.has(t) ? obs.get(t) : null);
        forecast.push(fc.has(t) ? fc.get(t) : null);
      }
    }
    if (sumEl) {
      const fcVals = fcTs.map(t => fc.get(t));
      if (fcErr) sumEl.innerHTML += ' • previsione OpenMeteo non disponibile';
      else if (fcVals.length) sumEl.innerHTML += ` • previsione OpenMeteo prossime 24h (stima modello): ` +
        `${fcVals.reduce((a, v) => a + v, 0).toFixed(1)} mm su ${fcVals.length} blocchi 3h completi`;
    }

    const C = _chartColors();
    if (_chartCum3) _chartCum3.destroy();
    _chartCum3 = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          { type: 'line', label: 'CUM3 osservato (pluviometri DPC interpolati)',
            data: observed, borderColor: C.cum3, spanGaps: false,
            backgroundColor: C.cum3 + '33', fill: true, tension: 0.2, pointRadius: 1.5 },
          { type: 'line', label: 'OpenMeteo previsione prossime 24h (somma 3h, stima modello)',
            data: forecast, borderColor: C.fc, spanGaps: false,
            backgroundColor: C.fc + '22', borderDash: [4, 3], fill: false, tension: 0.3, pointRadius: 2 },
        ],
      },
      options: { ..._commonChartOpts('mm/3h'),
        plugins: { ..._commonChartOpts('mm/3h').plugins,
          legend: { display: true, labels: { color: _cssVar('--text2', '#aaa'), font: { size: 10 } }, position: 'bottom' } },
      },
    });
  }

  function _commonChartOpts(yLabel) {
    const txt  = _cssVar('--text2', '#aaa');
    const grid = _isLight() ? 'rgba(0,0,0,0.08)' : 'rgba(255,255,255,0.05)';
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(20,20,30,0.95)',
          titleColor: '#fff', bodyColor: '#ddd', borderColor: '#444', borderWidth: 1,
          filter: item => item.parsed.y != null || item.datasetIndex === 0,
          callbacks: {
            label: item => {
              // Osservato senza valore → "—" esplicito; previsione fuori orizzonte → riga omessa
              if (item.parsed.y == null) return item.datasetIndex === 0 ? `${item.dataset.label}: — (nessun dato)` : null;
              return `${item.dataset.label}: ${item.parsed.y.toFixed(2)} mm`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: txt, maxTicksLimit: 12, autoSkip: true },
             grid:  { color: grid } },
        y: { title: { display: true, text: yLabel, color: txt },
             ticks: { color: txt },
             grid:  { color: grid },
             beginAtZero: true },
      },
    };
  }

  return { init, selectArea };
})();
