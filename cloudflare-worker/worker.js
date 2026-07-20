/**
 * Cloudflare Worker — Proxy CORS per Radar DPC
 *
 * Inoltra richieste al bucket S3 dpc-radar e all'API DPC aggiungendo header
 * CORS. Emula un browser reale per evitare il WAF di CloudFront.
 *
 * Endpoint:
 *   GET  /              → health check, mostra info worker
 *   *    /?url=<URL>    → proxy verso URL (whitelist enforced)
 */

const ALLOWED_HOSTS = [
  'dpc-radar.s3.eu-south-1.amazonaws.com',
  'radar-api.protezionecivile.it',
  // Composito radar ARPA Lombardia (Desio+Flero, CMPyymmddhhMM.MAX.tif.gz):
  // usato da arpa.html per visualizzare il segnale radar live sulla mappa.
  'radarlive.arpalombardia.it',
];

// User-Agent realistico: emula Chrome desktop per non essere bloccati dai WAF
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
                   'AppleWebKit/537.36 (KHTML, like Gecko) ' +
                   'Chrome/120.0.0.0 Safari/537.36';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': '*',
  'Access-Control-Expose-Headers': '*',
  'Access-Control-Max-Age': '86400',
};

export default {
  async fetch(request) {
    // Preflight CORS
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);

    // Health check: nessun parametro url
    if (!url.searchParams.has('url')) {
      const body = JSON.stringify({
        service: 'radar-dpc-proxy',
        status: 'ok',
        usage: 'append ?url=<encoded URL> to proxy a request',
        allowedHosts: ALLOWED_HOSTS,
        timestamp: new Date().toISOString(),
      }, null, 2);
      return new Response(body, {
        status: 200,
        headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
      });
    }

    const target = url.searchParams.get('url');
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return jsonError('Invalid url parameter (not a valid URL)', 400);
    }

    if (!ALLOWED_HOSTS.includes(targetUrl.hostname)) {
      return jsonError(`Hostname not allowed: ${targetUrl.hostname}`, 403);
    }

    // Costruisci gli header da inviare upstream
    const upstreamHeaders = {
      'User-Agent': BROWSER_UA,
      'Accept': '*/*',
      'Accept-Language': 'it-IT,it;q=0.9,en;q=0.8',
    };
    if (request.method === 'POST') {
      upstreamHeaders['Content-Type'] = request.headers.get('Content-Type') || 'application/json';
    }

    try {
      const upstream = await fetch(targetUrl.toString(), {
        method: request.method,
        headers: upstreamHeaders,
        body: request.method === 'POST' ? await request.text() : undefined,
        // Disabilita cache aggressiva del worker per non incagliarsi su URL pre-signed scadute
        cf: { cacheTtl: 60, cacheEverything: false },
      });

      const body = await upstream.arrayBuffer();

      // Costruisci gli header di risposta: parti da quelli upstream, aggiungi CORS
      const headers = new Headers();
      upstream.headers.forEach((v, k) => {
        // Salta header problematici/inutili
        const kl = k.toLowerCase();
        if (kl === 'set-cookie' || kl === 'transfer-encoding') return;
        headers.set(k, v);
      });
      Object.entries(CORS_HEADERS).forEach(([k, v]) => headers.set(k, v));

      // Cache leggera lato browser per i prodotti radar immutabili
      // (identificati da timestamp nel nome). Copre sia i .tif DPC/pre-signed
      // sia i .tif.gz del composito ARPA Lombardia.
      const p = targetUrl.pathname.toLowerCase();
      if (p.endsWith('.tif') || p.endsWith('.tif.gz')) {
        headers.set('Cache-Control', 'public, max-age=300');
      }

      return new Response(body, { status: upstream.status, headers });
    } catch (err) {
      return jsonError(`Upstream error: ${err.message || err}`, 502);
    }
  },
};

function jsonError(message, status) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}
