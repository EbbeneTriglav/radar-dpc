/**
 * api.js — Wrapper per l'API REST Radar DPC
 * https://radar-api.protezionecivile.it
 */

const RadarAPI = (() => {
  const { BASE, LAST, DOWNLOAD } = CONFIG.API;

  /** Ritorna l'ultimo timestamp disponibile per un prodotto */
  async function getLastProduct(type) {
    const res = await fetch(`${BASE}${LAST}?type=${type}`);
    if (!res.ok) throw new Error(`API ${res.status}`);
    const data = await res.json();
    if (!data.lastProducts?.length) throw new Error('Nessun prodotto disponibile');
    return data.lastProducts[0]; // { productType, time, period }
  }

  /** Ottiene la pre-signed URL S3 per un prodotto e timestamp */
  async function getDownloadUrl(productType, productDate) {
    const res = await fetch(`${BASE}${DOWNLOAD}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ productType, productDate }),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch {}
      throw new Error(msg);
    }
    return res.json(); // { bucket, key, url, expiresSeconds }
  }

  /**
   * Scarica il GeoTIFF come ArrayBuffer dalla pre-signed URL S3.
   * Gestisce il caso CORS aggiungendo una query string opaca.
   */
  async function fetchGeoTiff(url) {
    // Tentativo diretto
    try {
      const res = await fetch(url, { mode: 'cors' });
      if (!res.ok) throw new Error(`S3 ${res.status}`);
      return await res.arrayBuffer();
    } catch (e) {
      throw new Error(`Download GeoTIFF fallito: ${e.message}`);
    }
  }

  /**
   * Pipeline completa: tipo → URL S3 → ArrayBuffer
   * Usa cache locale per evitare download duplicati
   */
  const _cache = new Map();

  async function loadGeoTiff(productType, productDate) {
    const key = `${productType}_${productDate}`;
    if (_cache.has(key)) return _cache.get(key);

    const { url } = await getDownloadUrl(productType, productDate);
    const buffer = await fetchGeoTiff(url);

    // Mantieni max 20 entry in cache (FIFO)
    if (_cache.size >= 20) {
      const firstKey = _cache.keys().next().value;
      _cache.delete(firstKey);
    }
    _cache.set(key, buffer);
    return buffer;
  }

  /**
   * Costruisce un array di timestamp storici per animazione
   * @param {number} lastTs  - ultimo timestamp ms (epoch)
   * @param {number} stepMs  - passo del prodotto in ms
   * @param {number} count   - numero di frame da caricare
   */
  function buildTimestamps(lastTs, stepMs, count) {
    return Array.from({ length: count }, (_, i) => lastTs - (count - 1 - i) * stepMs);
  }

  /**
   * Geocoding via Nominatim OSM
   * @param {string} query - nome località o coordinate "lat,lon"
   */
  async function geocode(query) {
    // Tenta parsing coordinate dirette
    const coordMatch = query.match(/^(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)$/);
    if (coordMatch) {
      const lat = parseFloat(coordMatch[1]);
      const lon = parseFloat(coordMatch[2]);
      if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
        return [{ lat, lon, display_name: `${lat.toFixed(5)}, ${lon.toFixed(5)}` }];
      }
    }

    const params = new URLSearchParams({
      q: query,
      format: 'json',
      limit: 5,
      countrycodes: 'it',
      addressdetails: 1,
    });
    const res = await fetch(`${CONFIG.GEOCODING}?${params}`, {
      headers: { 'Accept-Language': 'it' },
    });
    if (!res.ok) throw new Error('Geocoding fallito');
    const results = await res.json();
    return results.map(r => ({
      lat: parseFloat(r.lat),
      lon: parseFloat(r.lon),
      display_name: r.display_name,
    }));
  }

  return { getLastProduct, getDownloadUrl, fetchGeoTiff, loadGeoTiff, buildTimestamps, geocode };
})();
