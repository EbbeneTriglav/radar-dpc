/**
 * basemap-picker.js — Controllo Leaflet per selezione basemap
 *
 * Espone 5 layer base gratuiti:
 *   - CARTO Dark      (default scuro)
 *   - CARTO Light     (default chiaro)
 *   - OpenStreetMap   (standard)
 *   - OSM Humanitario (Humanitarian OSM Team — strade più chiare in aree rurali)
 *   - Satellite Esri  (imagery satellitare ad alta risoluzione)
 *
 * API:
 *   BasemapPicker.init(map, defaultName?)
 *     → crea i layer, aggiunge il default alla mappa, monta il L.control.layers
 *
 *   BasemapPicker.applyTheme('dark' | 'light')
 *     → se il layer corrente è CARTO Dark/Light, switcha al corrispondente.
 *       Se l'utente ha scelto un altro layer (OSM, Satellite, ecc), non tocca nulla.
 */

const BasemapPicker = (() => {

  const LAYERS_DEF = [
    {
      name: 'CARTO Dark',
      url:  'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
      attr: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap contributors',
      opts: { subdomains: 'abcd', maxZoom: 19 },
    },
    {
      name: 'CARTO Light',
      url:  'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
      attr: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap contributors',
      opts: { subdomains: 'abcd', maxZoom: 19 },
    },
    {
      name: 'OpenStreetMap',
      url:  'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      attr: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      opts: { maxZoom: 19 },
    },
    {
      name: 'OSM Humanitario',
      url:  'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png',
      attr: '&copy; OpenStreetMap contributors, Tiles by <a href="https://www.hotosm.org/">HOT</a>',
      opts: { maxZoom: 19 },
    },
    {
      name: 'Satellite (Esri)',
      url:  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      attr: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
      opts: { maxZoom: 19 },
    },
  ];

  const _layers = {};
  let _currentName = null;
  let _map = null;

  function init(map, defaultName) {
    _map = map;

    LAYERS_DEF.forEach(({ name, url, attr, opts }) => {
      _layers[name] = L.tileLayer(url, { attribution: attr, ...opts });
    });

    const def = defaultName && _layers[defaultName]
      ? defaultName
      : (document.body.classList.contains('light-theme') ? 'CARTO Light' : 'CARTO Dark');

    _currentName = def;
    _layers[def].addTo(map);

    L.control.layers(_layers, null, {
      position: 'topright',
      collapsed: true,
    }).addTo(map);

    map.on('baselayerchange', (e) => { _currentName = e.name; });
  }

  function applyTheme(theme) {
    if (!_map || !_currentName) return;
    const target = theme === 'light' ? 'CARTO Light' : 'CARTO Dark';
    const opposite = theme === 'light' ? 'CARTO Dark' : 'CARTO Light';
    if (_currentName === opposite) {
      _map.removeLayer(_layers[opposite]);
      _layers[target].addTo(_map);
      _currentName = target;
    }
  }

  function currentLayerName() { return _currentName; }

  return { init, applyTheme, currentLayerName };
})();
