/**
 * basemap-picker.js — Controllo Leaflet per selezione basemap
 *
 * Espone 5 layer base gratuiti (nessuna API key):
 *   - Grigio scuro Esri  (default scuro)
 *   - Grigio chiaro Esri (default chiaro)
 *   - OpenStreetMap   (standard)
 *   - OSM Humanitario (Humanitarian OSM Team — strade più chiare in aree rurali)
 *   - Satellite Esri  (imagery satellitare ad alta risoluzione)
 *
 * API:
 *   BasemapPicker.init(map, defaultName?)
 *     → crea i layer, aggiunge il default alla mappa, monta il L.control.layers
 *
 *   BasemapPicker.applyTheme('dark' | 'light')
 *     → se il layer corrente è Grigio scuro/chiaro, switcha al corrispondente.
 *       Se l'utente ha scelto un altro layer (OSM, Satellite, ecc), non tocca nulla.
 */

const BasemapPicker = (() => {

  // CARTO da set-2026 richiede API key (watermark "API KEY REQUIRED"):
  // sostituito con Esri Gray Canvas (nessuna key).
  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
  const ESRI_ATTR = 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors';
  const LAYERS_DEF = [
    {
      name: 'Grigio scuro (Esri)',
      url:  ESRI + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
      labels: ESRI + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
      attr: ESRI_ATTR,
      opts: { maxZoom: 16 },
    },
    {
      name: 'Grigio chiaro (Esri)',
      url:  ESRI + 'World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
      labels: ESRI + 'World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
      attr: ESRI_ATTR,
      opts: { maxZoom: 16 },
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

    // Pane dedicati: basemap (tilePane, z 200) < radar (350) < etichette (380).
    // Così il radar resta SEMPRE sopra qualunque basemap scelta.
    if (!map.getPane('radar'))  { map.createPane('radar');  map.getPane('radar').style.zIndex  = 350; }
    if (!map.getPane('labels')) { map.createPane('labels'); map.getPane('labels').style.zIndex = 380;
                                  map.getPane('labels').style.pointerEvents = 'none'; }

    LAYERS_DEF.forEach(({ name, url, labels, attr, opts }) => {
      const base = L.tileLayer(url, { attribution: attr, ...opts });
      _layers[name] = labels
        ? L.layerGroup([base, L.tileLayer(labels, { pane: 'labels', ...opts })])
        : base;
    });

    const def = defaultName && _layers[defaultName]
      ? defaultName
      : (document.body.classList.contains('light-theme') ? 'Grigio chiaro (Esri)' : 'Grigio scuro (Esri)');

    _currentName = def;
    _layers[def].addTo(map);

    L.control.layers(_layers, null, {
      position: 'topright',
      collapsed: true,
      autoZIndex: false,   // evita che la basemap scelta finisca sopra il radar
    }).addTo(map);

    map.on('baselayerchange', (e) => { _currentName = e.name; });
  }

  function applyTheme(theme) {
    if (!_map || !_currentName) return;
    const target = theme === 'light' ? 'Grigio chiaro (Esri)' : 'Grigio scuro (Esri)';
    const opposite = theme === 'light' ? 'Grigio scuro (Esri)' : 'Grigio chiaro (Esri)';
    if (_currentName === opposite) {
      _map.removeLayer(_layers[opposite]);
      _layers[target].addTo(_map);
      _currentName = target;
    }
  }

  function currentLayerName() { return _currentName; }

  return { init, applyTheme, currentLayerName };
})();
