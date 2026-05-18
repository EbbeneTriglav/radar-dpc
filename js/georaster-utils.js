/**
 * georaster-utils.js
 * Caricamento GeoTIFF, rendering su Leaflet tramite GeoRasterLayer,
 * estrazione valore puntuale con buffer circolare.
 *
 * PROIEZIONI DPC (da gdalinfo ufficiale):
 *  - VMI, SRI, SRT1, CAPPI_*, VIL, ETM, POH, IR_108:
 *      Transverse Mercator custom — lat_0=42, lon_0=12.5, x_0=0, y_0=0
 *      Pixel: 1000m × 1000m, griglia 1200×1400
 *      Origin: (-600000, 650000) in coordinate TM [m]
 *
 *  - CUM3, CUM6, CUM12, CUM24, TEMP:
 *      WGS84 (EPSG:4326), coordinate geografiche [gradi]
 *      Pixel: ~0.01° × 0.01°
 *      NoData: -9999
 */

const GeoRasterUtils = (() => {

  // ─── Definizione proiezione TM custom DPC ────────────────────────────────
  // Fonte: metadati gdalinfo ufficiali DPC
  const DPC_TM_PROJ4 = '+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs';
  const WGS84_PROJ4  = '+proj=longlat +datum=WGS84 +no_defs';

  // Registra la proiezione in proj4 (se disponibile)
  if (typeof proj4 !== 'undefined') {
    proj4.defs('DPC_TM', DPC_TM_PROJ4);
  }

  /**
   * Determina se il georaster è in WGS84 geografico (gradi) o TM (metri).
   * CUM e TEMP hanno pixel ~0.01°; VMI/SRI/CAPPI hanno pixel ~1000m.
   */
  function _isWGS84(georaster) {
    return Math.abs(georaster.pixelWidth) < 1; // < 1 = gradi; > 1 = metri
  }

  /**
   * Converte lat/lon (WGS84) in coordinate native del georaster.
   * @returns {[number, number]} [x_native, y_native]
   */
  function _wgs84ToNative(georaster, lat, lon) {
    if (_isWGS84(georaster)) return [lon, lat]; // già in gradi
    if (typeof proj4 === 'undefined') {
      throw new Error('proj4.js richiesto per prodotti in proiezione TM');
    }
    return proj4(WGS84_PROJ4, DPC_TM_PROJ4, [lon, lat]);
  }

  /**
   * Converte coordinate native → lat/lon WGS84.
   */
  function _nativeToWGS84(georaster, x, y) {
    if (_isWGS84(georaster)) return [y, x]; // [lat, lon]
    if (typeof proj4 === 'undefined') return [null, null];
    const [lon, lat] = proj4(DPC_TM_PROJ4, WGS84_PROJ4, [x, y]);
    return [lat, lon];
  }

  // ─── Distanza Haversine in km ─────────────────────────────────────────────
  function _haversine(lat1, lon1, lat2, lon2) {
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  // ─── Stato modulo ─────────────────────────────────────────────────────────
  let _activeLayer = null;
  let _currentGeoRaster = null;
  let _map = null;
  let _opacity = 0.8;

  function init(mapInstance) {
    _map = mapInstance;
  }

  // ─── Parsing GeoTIFF ──────────────────────────────────────────────────────
  // georaster v1.6.0 accetta direttamente ArrayBuffer; internamente usa
  // geotiff.js per il parsing. Non passare un oggetto GeoTIFF già parsato:
  // la firma corretta è parseGeoraster(input, metadata?, debug?).
  async function parseGeoTiff(arrayBuffer) {
    try {
      const georaster = await parseGeoraster(arrayBuffer);
      return georaster;
    } catch (err) {
      console.error('[georaster] parseGeoraster fallito:', err);
      console.error('[georaster] buffer size:', arrayBuffer?.byteLength, 'first bytes:',
        new Uint8Array(arrayBuffer.slice(0, 8)));
      throw new Error('Parsing GeoTIFF fallito: ' + (err.message || err));
    }
  }

  // ─── Rendering su Leaflet ─────────────────────────────────────────────────
  async function showLayer(georaster, scaleName, opacity = _opacity) {
    _opacity = opacity;
    _currentGeoRaster = georaster;

    if (_activeLayer) {
      _map.removeLayer(_activeLayer);
      _activeLayer = null;
    }

    const noDataValue = georaster.noDataValue ?? -9999;
    const colorFn = ColorMap.makeColorFn(scaleName, noDataValue);

    _activeLayer = new GeoRasterLayer({
      georaster,
      opacity,
      pixelValuesToColorFn: colorFn,
      resolution: 256,
      // proj4 viene usato automaticamente da georaster-layer-for-leaflet
      // se caricato globalmente (gestisce la proiezione TM custom)
    });

    _activeLayer.addTo(_map);
    return _activeLayer;
  }

  function setOpacity(opacity) {
    _opacity = opacity;
    if (_activeLayer) _activeLayer.setOpacity(opacity);
  }

  function removeLayer() {
    if (_activeLayer) {
      _map.removeLayer(_activeLayer);
      _activeLayer = null;
    }
  }

  async function loadAndShow(productType, timestamp, scaleName, opacity) {
    const buffer = await RadarAPI.loadGeoTiff(productType, timestamp);
    const georaster = await parseGeoTiff(buffer);
    await showLayer(georaster, scaleName, opacity ?? _opacity);
    return georaster;
  }

  // ─── Estrazione buffer circolare ──────────────────────────────────────────
  /**
   * Estrae il valore medio in un cerchio di radiusKm attorno a centerLat/Lon.
   * Gestisce correttamente entrambe le proiezioni DPC:
   *   - TM custom (VMI, SRI, CAPPI…): pixel 1km, distanza in metri
   *   - WGS84 (CUM3/6/12/24, TEMP): pixel ~0.01°, distanza Haversine
   *
   * @returns {{ mean, count, min, max, projection }}
   */
  function extractBuffer(georaster, centerLat, centerLon, radiusKm = CONFIG.BUFFER_KM) {
    const { xmin, ymax, width, height, values, noDataValue } = georaster;
    const ndv = noDataValue ?? -9999;

    if (!values || !values[0]) return { mean: null, count: 0, min: null, max: null };
    const band = values[0];

    const wgs84 = _isWGS84(georaster);
    const pixW  = georaster.pixelWidth;   // sempre positivo in georaster
    const pixH  = georaster.pixelHeight;  // sempre positivo

    // Centro in coordinate native
    let [cx, cy] = _wgs84ToNative(georaster, centerLat, centerLon);

    // Pixel del centro
    const cPx = (cx - xmin) / pixW;
    const cPy = (ymax - cy) / pixH;

    // Buffer in pixel (stima conservativa)
    let bufPx, bufPy;
    if (wgs84) {
      // Gradi: 1° lat ≈ 111 km
      bufPx = Math.ceil((radiusKm / 111.0) / pixW) + 1;
      bufPy = Math.ceil((radiusKm / 111.0) / pixH) + 1;
    } else {
      // Metri: pixel = 1000 m
      const radiusM = radiusKm * 1000;
      bufPx = Math.ceil(radiusM / pixW) + 1;
      bufPy = Math.ceil(radiusM / pixH) + 1;
    }

    let sum = 0, count = 0, min = null, max = null;

    const pyMin = Math.max(0, Math.floor(cPy - bufPy));
    const pyMax = Math.min(height - 1, Math.ceil(cPy + bufPy));
    const pxMin = Math.max(0, Math.floor(cPx - bufPx));
    const pxMax = Math.min(width - 1, Math.ceil(cPx + bufPx));

    for (let py = pyMin; py <= pyMax; py++) {
      const row = band[py];
      if (!row) continue;

      for (let px = pxMin; px <= pxMax; px++) {
        // Coordinata centro pixel (native)
        const pixX = xmin + (px + 0.5) * pixW;
        const pixY = ymax - (py + 0.5) * pixH;

        // Distanza dal punto di interesse
        let distKm;
        if (wgs84) {
          // pixX = lon, pixY = lat in WGS84
          distKm = _haversine(centerLat, centerLon, pixY, pixX);
        } else {
          // Distanza euclidea in metri → km (TM è conforme, accurato a queste scale)
          const dM = Math.sqrt((pixX - cx) ** 2 + (pixY - cy) ** 2);
          distKm = dM / 1000;
        }

        if (distKm > radiusKm) continue;

        const val = row[px];
        if (val === undefined || val === null || val === ndv || isNaN(val) || val < -900) continue;

        sum += val;
        count++;
        if (min === null || val < min) min = val;
        if (max === null || val > max) max = val;
      }
    }

    return {
      mean: count > 0 ? sum / count : null,
      count,
      min,
      max,
      projection: wgs84 ? 'WGS84' : 'DPC_TM',
    };
  }

  function getCurrent() { return _currentGeoRaster; }

  return {
    init, parseGeoTiff, showLayer, setOpacity, removeLayer,
    loadAndShow, extractBuffer, getCurrent,
  };
})();
