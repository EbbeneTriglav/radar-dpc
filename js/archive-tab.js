/**
 * archive-tab.js — Tab "Archivio" dell'app principale
 *
 * Mostra i dati storici raccolti automaticamente dallo script
 * archive/scripts/collect.py per le aree configurate.
 *
 * Funzionalità:
 *   - selettore area (Ruspino / Panna / Cepina)
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
  const AREAS_URL    = 'archive/areas.json';
  const DATA_BASE    = 'archive/data';
  const IDW_POWER    = 2;     // esponente IDW: 2 dà transizioni morbide
  const IDW_PIXEL_PX = 4;     // dimensione "pixel" canvas in pixel CSS
  const CHART_COLORS = {
    cum24: '#7bed9f',  // verde menta
    cum3:  '#3eaaff',  // blu
  };

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

  // ─── Init ─────────────────────────────────────────────────────────────────
  async function init() {
    const panel = document.getElementById('tab-archive');
    if (!panel) return;

    // Carica config aree
    try {
      const r = await fetch(AREAS_URL);
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
        </div>
        <div class="archive-col-charts">
          <div class="archive-chart-wrap">
            <div class="archive-chart-title">
              CUM24 — Cumulata 24h <span id="archive-cum24-summary"></span>
            </div>
            <canvas id="archive-chart-cum24"></canvas>
          </div>
          <div class="archive-chart-wrap">
            <div class="archive-chart-title">
              CUM3 — Cumulata 3h (8 valori/giorno) <span id="archive-cum3-summary"></span>
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
    const r = await fetch(url, { cache: 'no-cache' });
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
    const dates24 = areaRows24.map(r => r.timestamp_utc.slice(0, 10)).sort();
    const dates3  = areaRows3.map(r  => r.timestamp_utc.slice(0, 10)).sort();
    info.innerHTML = `CUM24: ${areaRows24.length} giorni${dates24.length ? ` (${dates24[0]}…${dates24[dates24.length-1]})` : ''}` +
                     ` • CUM3: ${areaRows3.length} record`;
  }

  // ─── Mini-mappa con poligono e arealizzazione IDW ────────────────────────
  function _renderMiniMap() {
    if (!_currentArea) return;
    const area = _areasConfig.areas.find(a => a.name === _currentArea);
    if (!area) return;

    if (!_miniMap) {
      _miniMap = L.map('archive-map', { zoomControl: true, attributionControl: false });
      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '© CARTO', subdomains: 'abcd',
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

    // Usa il giorno più recente disponibile
    _selectedDateMs = days[days.length - 1];
    const frames = _framesForDay(cum3, _selectedDateMs);
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
      const mean = frame.area?.mean ? parseFloat(frame.area.mean).toFixed(1) : '—';
      label.textContent = `${tsStr} • ${mean} mm`;
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

    // Trova min/max per la scala colori dinamica (rain rate)
    const vals = pts.map(p => p.value);
    const vmax = Math.max(...vals, 0.1);

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
        const [r, g, b, a] = _valueToRGBA(v, vmax);
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

  /** Scala colori semplificata blu→verde→giallo→rosso per mm di pioggia. */
  function _valueToRGBA(v, vmax) {
    if (!isFinite(v) || v <= 0.05) return [0, 0, 0, 0];
    const t = Math.min(1, v / vmax);
    // Gradient stops
    const stops = [
      [0.00, [100, 170, 255, 100]],
      [0.25, [  0, 220, 220, 160]],
      [0.50, [  0, 210,   0, 200]],
      [0.75, [255, 200,   0, 220]],
      [1.00, [255,  60,   0, 240]],
    ];
    for (let i = 0; i < stops.length - 1; i++) {
      const [t0, c0] = stops[i];
      const [t1, c1] = stops[i + 1];
      if (t >= t0 && t <= t1) {
        const k = (t - t0) / (t1 - t0);
        return c0.map((ch, j) => Math.round(ch + k * (c1[j] - ch)));
      }
    }
    return stops[stops.length - 1][1];
  }

  // ─── Charts ───────────────────────────────────────────────────────────────
  function _renderCharts() {
    _renderChartCum24();
    _renderChartCum3();
  }

  function _renderChartCum24() {
    const ctx = document.getElementById('archive-chart-cum24');
    if (!ctx) return;
    const rows = (_data[_currentArea]?.cum24 || []).filter(r => r.location_type === 'area');
    const labels = rows.map(r => {
      const ms = new Date(r.timestamp_utc).getTime();
      return (typeof Timezone !== 'undefined')
        ? Timezone.format(ms, { day: '2-digit', month: '2-digit' })
        : r.timestamp_utc.slice(0, 10);
    });
    const data = rows.map(r => r.mean ? parseFloat(r.mean) : null);

    const sum   = data.filter(v => v != null).reduce((a, v) => a + v, 0);
    const max   = data.filter(v => v != null).reduce((a, v) => Math.max(a, v), 0);
    const ndays = data.filter(v => v != null && v > 0.1).length;
    document.getElementById('archive-cum24-summary').textContent =
      `totale ${sum.toFixed(1)} mm • max ${max.toFixed(1)} mm • ${ndays} giorni con pioggia`;

    if (_chartCum24) _chartCum24.destroy();
    _chartCum24 = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'CUM24 medio area (mm)',
          data,
          backgroundColor: CHART_COLORS.cum24 + 'cc',
          borderColor: CHART_COLORS.cum24,
          borderWidth: 1,
        }],
      },
      options: _commonChartOpts('mm'),
    });
  }

  async function _renderChartCum3() {
    const ctx = document.getElementById('archive-chart-cum3');
    if (!ctx) return;
    const rows = (_data[_currentArea]?.cum3 || [])
      .filter(r => r.location_type === 'area')
      .sort((a, b) => a.timestamp_utc.localeCompare(b.timestamp_utc));

    // Costruisci labels timeline
    const labels = rows.map(r => {
      const ms = new Date(r.timestamp_utc).getTime();
      return (typeof Timezone !== 'undefined')
        ? Timezone.format(ms, { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' })
        : r.timestamp_utc.slice(0, 16).replace('T', ' ');
    });
    const data = rows.map(r => r.mean ? parseFloat(r.mean) : null);

    const sum = data.filter(v => v != null).reduce((a, v) => a + v, 0);
    const max = data.filter(v => v != null).reduce((a, v) => Math.max(a, v), 0);
    document.getElementById('archive-cum3-summary').textContent =
      `${rows.length} record • totale ${sum.toFixed(1)} mm • picco 3h ${max.toFixed(1)} mm`;

    // OpenMeteo forecast prossime 24h come overlay (linea blu chiaro)
    let omLabels = [], omData = [];
    try {
      const area = _areasConfig.areas.find(a => a.name === _currentArea);
      if (area) {
        const r = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${area.centroid.lat}` +
          `&longitude=${area.centroid.lon}&minutely_15=precipitation&forecast_minutes=1440&timezone=UTC`,
          { cache: 'no-cache' });
        const d = await r.json();
        const ts = d.minutely_15?.time || [];
        const pr = d.minutely_15?.precipitation || [];
        // Aggrega in cumulate 3h per matchare la granularità DPC
        for (let i = 0; i + 12 <= ts.length; i += 12) {
          const ms = new Date(ts[i] + 'Z').getTime();
          const lbl = (typeof Timezone !== 'undefined')
            ? Timezone.format(ms, { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' })
            : ts[i].slice(0, 16).replace('T', ' ');
          omLabels.push(lbl);
          omData.push(pr.slice(i, i + 12).reduce((a, v) => a + (v || 0), 0));
        }
      }
    } catch (e) { console.warn('OM fetch err', e); }

    // Unifica gli assi: append forecast in coda alle observed
    const allLabels = labels.concat(omLabels);
    const observedAligned = data.concat(omData.map(() => null));
    const forecastAligned = labels.map(() => null).concat(omData);

    if (_chartCum3) _chartCum3.destroy();
    _chartCum3 = new Chart(ctx, {
      data: {
        labels: allLabels,
        datasets: [
          { type: 'line', label: 'DPC CUM3 osservato',
            data: observedAligned, borderColor: CHART_COLORS.cum3,
            backgroundColor: CHART_COLORS.cum3 + '33', fill: true, tension: 0.2, pointRadius: 2 },
          { type: 'line', label: 'OpenMeteo forecast 3h cumulata',
            data: forecastAligned, borderColor: '#82b1ff',
            backgroundColor: '#82b1ff22', borderDash: [4, 3], fill: false, tension: 0.3, pointRadius: 1 },
        ],
      },
      options: { ..._commonChartOpts('mm/3h'),
        plugins: { legend: { display: true, labels: { color: '#aaa', font: { size: 10 } }, position: 'bottom' } },
      },
    });
  }

  function _commonChartOpts(yLabel) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(20,20,30,0.95)',
          titleColor: '#fff', bodyColor: '#ddd', borderColor: '#444', borderWidth: 1,
          callbacks: {
            label: item => {
              if (item.parsed.y == null) return null;
              return `${item.dataset.label}: ${item.parsed.y.toFixed(2)} mm`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: '#aaa', maxTicksLimit: 12, autoSkip: true },
             grid:  { color: 'rgba(255,255,255,0.05)' } },
        y: { title: { display: true, text: yLabel, color: '#aaa' },
             ticks: { color: '#aaa' },
             grid:  { color: 'rgba(255,255,255,0.05)' },
             beginAtZero: true },
      },
    };
  }

  return { init, selectArea };
})();
