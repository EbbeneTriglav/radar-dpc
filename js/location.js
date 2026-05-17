/**
 * location.js — Gestione punti di interesse
 * Ricerca geocoding, click su mappa, buffer 2km, multi-punto.
 */

const LocationPanel = (() => {
  const MAX_POINTS = 3;
  const COLORS = ['#00e5ff', '#ff6b35', '#b2ff59'];

  let _map = null;
  let _points = []; // { id, lat, lon, label, marker, circle, color }
  let _onPointsChanged = null;
  let _geocodeTimeout = null;

  let elSearchInput, elSearchResults, elPointsList;

  function init(mapInstance, onPointsChanged) {
    _map = mapInstance;
    _onPointsChanged = onPointsChanged;

    elSearchInput   = document.getElementById('location-search');
    elSearchResults = document.getElementById('search-results');
    elPointsList    = document.getElementById('points-list');

    // Click su mappa → aggiunge punto
    _map.on('click', async (e) => {
      if (!document.getElementById('map-click-mode')?.checked) return;
      await addPoint(e.latlng.lat, e.latlng.lng);
    });

    // Ricerca con debounce
    elSearchInput?.addEventListener('input', () => {
      clearTimeout(_geocodeTimeout);
      const q = elSearchInput.value.trim();
      if (q.length < 3) { elSearchResults.innerHTML = ''; return; }
      _geocodeTimeout = setTimeout(() => _doSearch(q), 500);
    });

    // Chiudi risultati cliccando fuori
    document.addEventListener('click', (e) => {
      if (!elSearchInput?.contains(e.target)) {
        if (elSearchResults) elSearchResults.innerHTML = '';
      }
    });
  }

  async function _doSearch(query) {
    try {
      const results = await RadarAPI.geocode(query);
      _renderResults(results);
    } catch (e) {
      if (elSearchResults) elSearchResults.innerHTML = '<div class="sr-item sr-error">Geocoding non disponibile</div>';
    }
  }

  function _renderResults(results) {
    if (!elSearchResults) return;
    if (!results.length) {
      elSearchResults.innerHTML = '<div class="sr-item sr-empty">Nessun risultato</div>';
      return;
    }
    elSearchResults.innerHTML = results.map((r, i) => `
      <div class="sr-item" data-idx="${i}" data-lat="${r.lat}" data-lon="${r.lon}" data-name="${_escape(r.display_name)}">
        <i class="fa fa-map-marker-alt"></i>
        <span>${_escape(r.display_name)}</span>
      </div>
    `).join('');

    elSearchResults.querySelectorAll('.sr-item[data-lat]').forEach(el => {
      el.addEventListener('click', () => {
        const lat = parseFloat(el.dataset.lat);
        const lon = parseFloat(el.dataset.lon);
        const name = el.dataset.name;
        elSearchResults.innerHTML = '';
        elSearchInput.value = '';
        addPoint(lat, lon, name);
        _map.setView([lat, lon], 10, { animate: true });
      });
    });
  }

  async function addPoint(lat, lon, label = null) {
    if (_points.length >= MAX_POINTS) {
      showToast(`Massimo ${MAX_POINTS} punti contemporaneamente`, 'warn');
      return;
    }

    if (!label) label = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;

    const id = Date.now();
    const color = COLORS[_points.length % COLORS.length];

    // Marker personalizzato
    const markerIcon = L.divIcon({
      className: '',
      html: `<div class="custom-marker" style="background:${color}"></div>`,
      iconSize: [16, 16],
      iconAnchor: [8, 8],
    });

    const marker = L.marker([lat, lon], { icon: markerIcon })
      .addTo(_map)
      .bindPopup(`<b style="color:${color}">${label}</b><br>${lat.toFixed(5)}, ${lon.toFixed(5)}`);

    const circle = L.circle([lat, lon], {
      radius: CONFIG.BUFFER_KM * 1000,
      color,
      fillColor: color,
      fillOpacity: 0.08,
      weight: 1.5,
      dashArray: '5 4',
    }).addTo(_map);

    const point = { id, lat, lon, label, marker, circle, color };
    _points.push(point);
    _renderPointsList();
    _onPointsChanged?.([..._points]);
    return point;
  }

  function removePoint(id) {
    const idx = _points.findIndex(p => p.id === id);
    if (idx === -1) return;
    const p = _points[idx];
    p.marker.remove();
    p.circle.remove();
    _points.splice(idx, 1);
    _renderPointsList();
    _onPointsChanged?.([..._points]);
  }

  function clearAll() {
    [..._points].forEach(p => removePoint(p.id));
  }

  function _renderPointsList() {
    if (!elPointsList) return;
    if (!_points.length) {
      elPointsList.innerHTML = '<p class="no-points">Nessun punto selezionato.<br>Cerca una località o clicca sulla mappa (attiva modalità click).</p>';
      return;
    }
    elPointsList.innerHTML = _points.map(p => `
      <div class="point-item" data-id="${p.id}">
        <span class="point-dot" style="background:${p.color}"></span>
        <span class="point-label" title="${p.label}">${_truncate(p.label, 30)}</span>
        <span class="point-coords">${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}</span>
        <button class="btn-remove-point" data-id="${p.id}" title="Rimuovi"><i class="fa fa-times"></i></button>
      </div>
    `).join('');

    elPointsList.querySelectorAll('.btn-remove-point').forEach(btn => {
      btn.addEventListener('click', () => removePoint(parseInt(btn.dataset.id)));
    });
  }

  /**
   * Estrae i valori per tutti i punti dal georaster corrente.
   * @param {object} georaster
   * @returns {{ point, result }[]}
   */
  function extractAll(georaster) {
    return _points.map(p => ({
      point: p,
      result: GeoRasterUtils.extractBuffer(georaster, p.lat, p.lon),
    }));
  }

  function getPoints() { return [..._points]; }

  function _escape(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function _truncate(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }

  return { init, addPoint, removePoint, clearAll, extractAll, getPoints };
})();
