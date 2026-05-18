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
 *
 * RIPROIEZIONE PER IL RENDERING
 * --------------------------------------------------------------------------
 * georaster-layer-for-leaflet NON riproietta automaticamente la TM custom
 * del DPC (non è un EPSG standard). Senza fix, i prodotti TM verrebbero
 * disegnati con coordinate native in metri (xmin=-600000) su una mappa
 * che si aspetta gradi → pixel completamente fuori dal viewport →
 * il layer sembra "non esserci".
 *
 * Soluzione: dopo il parse calcoliamo i bounds in WGS84 con proj4 e
 * sovrascriviamo xmin/xmax/ymin/ymax/pixelWidth/pixelHeight del georaster.
 * Salviamo i valori originali in `_origTM` perché `extractBuffer` ha
 * bisogno delle coordinate native in metri per le interrogazioni puntuali.
 * La distorsione introdotta è < 0.1% per l'Italia centrale (trascurabile).
 */

const GeoRasterUtils = (() => {

  const DPC_TM_PROJ4 = '+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs';
  const WGS84_PROJ4  = '+proj=longlat +datum=WGS84 +no_defs';

  if (typeof proj4 !== 'undefined') {
    proj4.defs('DPC_TM', DPC_TM_PROJ4);
  }

  function _isWGS84(georaster) {
    return Math.abs(georaster.pixelWidth) < 1;
  }

  function _wgs84ToNative(georaster, lat, lon) {
    const origTM = georaster._origTM;
    if (origTM || georaster._origIsTM) {
      if (typeof proj4 === 'undefined') {
        throw new Error('proj4.js richiesto per prodotti in proiezione TM');
      }
      return proj4(WGS84_PROJ4, DPC_TM_PROJ4, [lon, lat]);
    }
    return [lon, lat];
  }

  function _haversine(lat1, lon1, lat2, lon2) {
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  // ─── Stato modulo ─────────────────────────────────────────────────────────
  let _activeLayer = null;
  let _currentGeoRaster = null;
  let _map = null;
  let _opacity = 0.8;

  function init(mapInstance) { _map = mapInstance; }

  // ─── Parsing GeoTIFF ──────────────────────────────────────────────────────
  async function parseGeoTiff(arrayBuffer) {
    try {
      const georaster = await parseGeoraster(arrayBuffer);

      // Salva PRIMA della eventuale mutazione se era originariamente TM
      georaster._origIsTM = !_isWGS84(georaster);

      // Log diagnostico
      const band0 = georaster.values?.[0];
      let valSample = 'n/a';
      if (band0 && band0[Math.floor(georaster.height / 2)]) {
        const row = band0[Math.floor(georaster.height / 2)];
        valSample = row[Math.floor(georaster.width / 2)];
      }
      console.log('[georaster] parsed', {
        size: `${georaster.width}×${georaster.height}`,
        bounds: [
          georaster.xmin.toFixed(2), georaster.ymin.toFixed(2),
          georaster.xmax.toFixed(2), georaster.ymax.toFixed(2),
        ].join(', '),
        pixel: [georaster.pixelWidth, georaster.pixelHeight],
        proj: georaster._origIsTM ? 'TM custom (DPC)' : 'WGS84',
        noData: georaster.noDataValue,
        bands: georaster.values?.length,
        sampleCenter: valSample,
        minMax: (georaster.mins && georaster.maxs)
          ? `${georaster.mins[0]} … ${georaster.maxs[0]}`
          : 'n/a',
      });
      return georaster;
    } catch (err) {
      console.error('[georaster] parseGeoraster fallito:', err);
      console.error('[georaster] buffer size:', arrayBuffer?.byteLength, 'first bytes:',
        new Uint8Array(arrayBuffer.slice(0, 8)));
      throw new Error('Parsing GeoTIFF fallito: ' + (err.message || err));
    }
  }

  /**
   * Se il georaster è in proiezione TM custom, riproietta i bounds a WGS84.
   * Mutazione idempotente.
   */
  function _ensureWGS84Bounds(georaster) {
    if (georaster._origTM || !georaster._origIsTM) return;
    if (typeof proj4 === 'undefined') {
      console.warn('[georaster] proj4 non disponibile: prodotti TM non saranno renderizzati');
      return;
    }

    const { xmin, xmax, ymin, ymax, width, height, pixelWidth, pixelHeight } = georaster;

    const sw = proj4(DPC_TM_PROJ4, WGS84_PROJ4, [xmin, ymin]);
    const ne = proj4(DPC_TM_PROJ4, WGS84_PROJ4, [xmax, ymax]);
    const nw = proj4(DPC_TM_PROJ4, WGS84_PROJ4, [xmin, ymax]);
    const se = proj4(DPC_TM_PROJ4, WGS84_PROJ4, [xmax, ymin]);

    georaster._origTM = { xmin, xmax, ymin, ymax, pixelWidth, pixelHeight };

    georaster.xmin = Math.min(sw[0], nw[0]);
    georaster.xmax = Math.max(ne[0], se[0]);
    georaster.ymin = Math.min(sw[1], se[1]);
    georaster.ymax = Math.max(nw[1], ne[1]);
    georaster.pixelWidth  = (georaster.xmax - georaster.xmin) / width;
    georaster.pixelHeight = (georaster.ymax - georaster.ymin) / height;

    console.log('[georaster] bounds TM→WGS84:',
      `lon ${georaster.xmin.toFixed(3)}…${georaster.xmax.toFixed(3)}`,
      `lat ${georaster.ymin.toFixed(3)}…${georaster.ymax.toFixed(3)}`);
  }

  // ─── Rendering su Leaflet ─────────────────────────────────────────────────
  async function showLayer(georaster, scaleName, opacity = _opacity) {
    _opacity = opacity;
    _currentGeoRaster = georaster;

    if (_activeLayer) {
      _map.removeLayer(_activeLayer);
      _activeLayer = null;
    }

    _ensureWGS84Bounds(georaster);

    const noDataValue = georaster.noDataValue ?? -9999;
    const colorFn = ColorMap.makeColorFn(scaleName, noDataValue);

    _activeLayer = new GeoRasterLayer({
      georaster,
      opacity,
      pixelValuesToColorFn: colorFn,
      resolution: 256,
    });

    _activeLayer.addTo(_map);
    console.log(`[georaster] layer aggiunto — scale: ${scaleName}, opacity: ${opacity}`);
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
  function extractBuffer(georaster, centerLat, centerLon, radiusKm = CONFIG.BUFFER_KM) {
    const { width, height, values, noDataValue } = georaster;
    const ndv = noDataValue ?? -9999;

    if (!values || !values[0]) return { mean: null, count: 0, min: null, max: null };
    const band = values[0];

    // Usa i bounds originali (TM in metri) se la riproiezione è avvenuta,
    // altrimenti quelli del georaster (WGS84 nativo).
    const orig = georaster._origTM || {
      xmin: georaster.xmin, ymax: georaster.ymax,
      pixelWidth: georaster.pixelWidth, pixelHeight: georaster.pixelHeight,
    };
    const wgs84 = !georaster._origIsTM;
    const pixW = orig.pixelWidth;
    const pixH = orig.pixelHeight;

    let cx, cy;
    if (wgs84) { cx = centerLon; cy = centerLat; }
    else       { [cx, cy] = _wgs84ToNative(georaster, centerLat, centerLon); }

    const cPx = (cx - orig.xmin) / pixW;
    const cPy = (orig.ymax - cy) / pixH;

    let bufPx, bufPy;
    if (wgs84) {
      bufPx = Math.ceil((radiusKm / 111.0) / pixW) + 1;
      bufPy = Math.ceil((radiusKm / 111.0) / pixH) + 1;
    } else {
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
        const pixX = orig.xmin + (px + 0.5) * pixW;
        const pixY = orig.ymax - (py + 0.5) * pixH;

        let distKm;
        if (wgs84) {
          distKm = _haversine(centerLat, centerLon, pixY, pixX);
        } else {
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
      count, min, max,
      projection: wgs84 ? 'WGS84' : 'DPC_TM',
    };
  }

  function getCurrent() { return _currentGeoRaster; }

  return {
    init, parseGeoTiff, showLayer, setOpacity, removeLayer,
    loadAndShow, extractBuffer, getCurrent,
  };
})();
