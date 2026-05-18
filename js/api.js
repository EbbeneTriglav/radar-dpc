/**
 * api.js — Wrapper per l'API REST Radar DPC
 * https://radar-api.protezionecivile.it
 *
 * Il bucket S3 dpc-radar.s3.eu-south-1.amazonaws.com NON ha header CORS
 * abilitati per origin esterni: il fetch diretto da GitHub Pages fallisce
 * sempre. Si usa quindi una catena di proxy CORS pubblici con fallback.
 *
 * IMPORTANTE: in produzione conviene sostituire la lista CORS_PROXIES con
 * un solo URL puntato a un proprio Cloudflare Worker (vedi
 * /cloudflare-worker/worker.js nel repo). 100k richieste/giorno gratis,
 * latenza < 50 ms, niente rate-limit dei proxy pubblici.
 */

const RadarAPI = (() => {
  const { BASE, LAST, DOWNLOAD } = CONFIG.API;

  // Catena di proxy CORS in ordine di preferenza.
  // Ogni elemento è una funzione che dato l'URL target ritorna l'URL proxato.
  // Verificati funzionanti a maggio 2026.
  const CORS_PROXIES = [
    // Se CONFIG.CORS_PROXY è impostato (es. Cloudflare Worker proprio) lo usa per primo
    ...(CONFIG.CORS_PROXY ? [(u) => CONFIG.CORS_PROXY + encodeURIComponent(u)] : []),
    (u) => `https://api.allorigins.win/raw?url=${encodeURIComponent(u)}`,
    (u) => `https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(u)}`,
    (u) => `https://proxy.cors.sh/${u}`,
  ];

  const FETCH_TIMEOUT_MS = 15000;

  /** Fetch con timeout (AbortController) */
  async function _fetchWithTimeout(url, opts = {}, timeoutMs = FETCH_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...opts, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  /** Ritorna l'ultimo timestamp disponibile per un prodotto */
  async function getLastProduct(type) {
    const res = await _fetchWithTimeout(`${BASE}${LAST}?type=${type}`);
    if (!res.ok) throw new Error(`API ${res.status}`);
    const data = await res.json();
    if (!data.lastProducts?.length) throw new Error('Nessun prodotto disponibile');
    return data.lastProducts[0]; // { productType, time, period }
  }

  /** Ottiene la pre-signed URL S3 per un prodotto e timestamp */
  async function getDownloadUrl(productType, productDate) {
    const res = await _fetchWithTimeout(`${BASE}${DOWNLOAD}`, {
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
   * Strategia:
   *   1. tenta fetch diretto (utile in localhost o se DPC abilita CORS)
   *   2. in caso di errore, itera i proxy CORS finché uno funziona
   */
  async function fetchGeoTiff(url) {
    // 1) tentativo diretto
    try {
      const res = await _fetchWithTimeout(url, { mode: 'cors' }, 8000);
      if (res.ok) return await res.arrayBuffer();
    } catch (_) {
      // CORS / rete → si passa ai proxy
    }

    // 2) catena di proxy
    let lastErr = null;
    for (let i = 0; i < CORS_PROXIES.length; i++) {
      const proxyUrl = CORS_PROXIES[i](url);
      try {
        const res = await _fetchWithTimeout(proxyUrl);
        if (!res.ok) {
          lastErr = new Error(`Proxy ${i} HTTP ${res.status}`);
          continue;
        }
        const buf = await res.arrayBuffer();
        if (buf.byteLength < 256) {
          // probabile risposta di errore HTML del proxy mascherata da 200
          lastErr = new Error(`Proxy ${i} risposta troppo piccola (${buf.byteLength} B)`);
          continue;
        }
        return buf;
      } catch (e) {
        lastErr = e;
        // continua col prossimo proxy
      }
    }
    throw new Error(`Download GeoTIFF fallito su tutti i proxy: ${lastErr?.message || 'unknown'}`);
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
   */
  function buildTimestamps(lastTs, stepMs, count) {
    return Array.from({ length: count }, (_, i) => lastTs - (count - 1 - i) * stepMs);
  }

  /**
   * Geocoding via Nominatim OSM
   */
  async function geocode(query) {
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
    const res = await _fetchWithTimeout(`${CONFIG.GEOCODING}?${params}`, {
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
