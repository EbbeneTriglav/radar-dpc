/**
 * api.js — Wrapper per l'API REST Radar DPC
 * https://radar-api.protezionecivile.it
 *
 * Strategia di download dei GeoTIFF da S3:
 *   - se CONFIG.CORS_PROXY è impostato (Cloudflare Worker proprio):
 *       chiama direttamente quello, senza nemmeno provare il fetch diretto
 *       che sappiamo già che fallisce sempre con CORS.
 *   - altrimenti: prova fetch diretto, poi catena di proxy pubblici.
 *
 * Per debug le richieste sono loggate su console come [api].
 */

const RadarAPI = (() => {
  const { BASE, LAST, DOWNLOAD } = CONFIG.API;

  const PUBLIC_PROXIES = [
    (u) => `https://api.allorigins.win/raw?url=${encodeURIComponent(u)}`,
    (u) => `https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(u)}`,
    (u) => `https://proxy.cors.sh/${u}`,
  ];

  const HAS_CUSTOM_PROXY = !!(CONFIG.CORS_PROXY && CONFIG.CORS_PROXY.length > 0);

  // Log all'avvio quale modalità sta usando (utile per debug)
  console.log('[api] init — CORS_PROXY:', HAS_CUSTOM_PROXY ? CONFIG.CORS_PROXY : '(nessuno, uso fallback pubblici)');

  const FETCH_TIMEOUT_MS = 20000;

  // Instrada un URL attraverso il proxy CORS se configurato. L'API DPC
  // (radar-api.protezionecivile.it) non manda header CORS, quindi le chiamate
  // dirette dal browser falliscono: vanno passate dal worker, che è già
  // abilitato per quell'host. Senza proxy configurato ritorna l'URL invariato.
  function _viaProxy(url) {
    return HAS_CUSTOM_PROXY ? CONFIG.CORS_PROXY + encodeURIComponent(url) : url;
  }

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
    const res = await _fetchWithTimeout(_viaProxy(`${BASE}${LAST}?type=${type}`));
    if (!res.ok) throw new Error(`API ${res.status}`);
    const data = await res.json();
    if (!data.lastProducts?.length) throw new Error('Nessun prodotto disponibile');
    return data.lastProducts[0];
  }

  /** Ottiene la pre-signed URL S3 per un prodotto e timestamp */
  async function getDownloadUrl(productType, productDate) {
    const res = await _fetchWithTimeout(_viaProxy(`${BASE}${DOWNLOAD}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ productType, productDate }),
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  /**
   * Scarica il GeoTIFF come ArrayBuffer dalla pre-signed URL S3.
   */
  async function fetchGeoTiff(url) {
    // === MODO 1: Cloudflare Worker proprio ===
    // Sappiamo che il fetch diretto fallisce sempre per CORS, quindi salta
    // direttamente sul worker. Niente fallback ai proxy pubblici (rate-limit).
    if (HAS_CUSTOM_PROXY) {
      const proxyUrl = CONFIG.CORS_PROXY + encodeURIComponent(url);
      console.log('[api] fetchGeoTiff via custom proxy:', proxyUrl.substring(0, 100) + '…');
      const res = await _fetchWithTimeout(proxyUrl);
      if (!res.ok) {
        throw new Error(`Custom proxy HTTP ${res.status}: ${await res.text().catch(() => '')}`);
      }
      const buf = await res.arrayBuffer();
      if (buf.byteLength < 256) {
        throw new Error(`Risposta proxy troppo piccola (${buf.byteLength} B), probabilmente un errore`);
      }
      console.log(`[api] fetchGeoTiff OK — ${buf.byteLength} B`);
      return buf;
    }

    // === MODO 2: proxy pubblici (catena) ===
    // 1) tentativo diretto (utile in localhost o future modifiche CORS DPC)
    try {
      const res = await _fetchWithTimeout(url, { mode: 'cors' }, 8000);
      if (res.ok) return await res.arrayBuffer();
    } catch (_) {}

    // 2) catena di proxy pubblici
    let lastErr = null;
    for (let i = 0; i < PUBLIC_PROXIES.length; i++) {
      const proxyUrl = PUBLIC_PROXIES[i](url);
      try {
        const res = await _fetchWithTimeout(proxyUrl);
        if (!res.ok) {
          lastErr = new Error(`Public proxy ${i} HTTP ${res.status}`);
          continue;
        }
        const buf = await res.arrayBuffer();
        if (buf.byteLength < 256) {
          lastErr = new Error(`Public proxy ${i} risposta troppo piccola (${buf.byteLength} B)`);
          continue;
        }
        return buf;
      } catch (e) {
        lastErr = e;
      }
    }
    throw new Error(`Download GeoTIFF fallito su tutti i proxy: ${lastErr?.message || 'unknown'}`);
  }

  /**
   * Pipeline completa: tipo → URL S3 → ArrayBuffer
   * Usa cache locale per evitare download duplicati.
   */
  const _cache = new Map();

  async function loadGeoTiff(productType, productDate) {
    const key = `${productType}_${productDate}`;
    if (_cache.has(key)) return _cache.get(key);

    const { url } = await getDownloadUrl(productType, productDate);
    const buffer = await fetchGeoTiff(url);

    if (_cache.size >= 20) {
      const firstKey = _cache.keys().next().value;
      _cache.delete(firstKey);
    }
    _cache.set(key, buffer);
    return buffer;
  }

  function buildTimestamps(lastTs, stepMs, count) {
    return Array.from({ length: count }, (_, i) => lastTs - (count - 1 - i) * stepMs);
  }

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
