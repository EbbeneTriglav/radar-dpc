/**
 * Cloudflare Worker — Proxy CORS dedicato per Radar DPC
 *
 * Inoltra le richieste al bucket S3 dpc-radar.s3.eu-south-1.amazonaws.com
 * aggiungendo gli header CORS necessari. Free tier: 100.000 richieste/giorno.
 *
 * DEPLOY in 5 minuti:
 *   1. Vai su https://dash.cloudflare.com → Workers & Pages → Create
 *   2. Scegli "Create Worker", dai un nome (es. radar-dpc-proxy)
 *   3. "Edit code" → incolla questo file → Save & Deploy
 *   4. Copia l'URL del worker (es. https://radar-dpc-proxy.tuonome.workers.dev/)
 *   5. Nel file js/config.js della piattaforma imposta:
 *        CORS_PROXY: 'https://radar-dpc-proxy.tuonome.workers.dev/?url='
 *
 * Vantaggi rispetto ai proxy pubblici:
 *   - 0 rate-limit per uso personale
 *   - latenza < 50 ms (edge CDN globale)
 *   - nessun MITM con servizi sconosciuti
 *   - nessun limite di dimensione file
 */

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Preflight CORS
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': '*',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    // Estrai URL target dal parametro ?url=
    const target = url.searchParams.get('url');
    if (!target) {
      return new Response('Missing ?url= parameter', { status: 400 });
    }

    // Whitelist domini ammessi (sicurezza: evita open-proxy)
    const allowed = [
      'dpc-radar.s3.eu-south-1.amazonaws.com',
      'radar-api.protezionecivile.it',
    ];
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response('Invalid url parameter', { status: 400 });
    }
    if (!allowed.includes(targetUrl.hostname)) {
      return new Response('Hostname not allowed', { status: 403 });
    }

    // Inoltra la richiesta
    try {
      const upstream = await fetch(targetUrl.toString(), {
        method: request.method,
        headers: request.method === 'POST'
          ? { 'Content-Type': request.headers.get('Content-Type') || 'application/json' }
          : undefined,
        body: request.method === 'POST' ? await request.text() : undefined,
      });

      const body = await upstream.arrayBuffer();
      const headers = new Headers(upstream.headers);
      headers.set('Access-Control-Allow-Origin', '*');
      headers.set('Access-Control-Expose-Headers', '*');
      // Cache aggressivo per i .tif (immutabili)
      if (targetUrl.pathname.endsWith('.tif')) {
        headers.set('Cache-Control', 'public, max-age=3600');
      }
      return new Response(body, { status: upstream.status, headers });
    } catch (err) {
      return new Response(`Upstream error: ${err.message}`, {
        status: 502,
        headers: { 'Access-Control-Allow-Origin': '*' },
      });
    }
  },
};
