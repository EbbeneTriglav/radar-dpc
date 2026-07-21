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

// ─── WATCHDOG WORKFLOW GITHUB ────────────────────────────────────────────────
// Il cron di GitHub Actions è "best-effort": nei momenti di carico i run
// slittano di 30-200 min o saltano (→ allerte mancate). I cron trigger di
// Cloudflare invece sono affidabili: questo watchdog, eseguito dal cron del
// Worker (configurare "*/10 * * * *" nei Triggers), controlla l'ultimo run di
// ogni workflow critico e, se è più vecchio della soglia, lo riavvia via
// workflow_dispatch. Il dispatch crea subito un run nuovo, quindi il check
// successivo lo vede fresco: auto-limitante, niente kick a raffica.
// Secret richiesto sul Worker: GH_TOKEN (PAT fine-grained, repo radar-dpc,
// permesso Actions: Read and write). Opzionali: TG_BOT_TOKEN + TG_CHAT_ID per
// notifica Telegram quando il watchdog riavvia qualcosa.
const GH_REPO = 'EbbeneTriglav/radar-dpc';
const WATCHED_WORKFLOWS = [
  { wf: 'monitor.yml',        maxAgeMin: 35 },   // cron ogni 15'
  { wf: 'nowcast.yml',        maxAgeMin: 45 },   // ogni 20'
  { wf: 'arpa-collect.yml',   maxAgeMin: 30 },   // ogni 10'
  { wf: 'forecast-alert.yml', maxAgeMin: 165 },  // ogni 2h
];

async function watchdog(env) {
  if (!env.GH_TOKEN) return;   // watchdog disattivo senza token
  const headers = {
    'Authorization': `Bearer ${env.GH_TOKEN}`,
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'radar-dpc-watchdog',
  };
  const kicked = [];
  for (const w of WATCHED_WORKFLOWS) {
    try {
      const r = await fetch(
        `https://api.github.com/repos/${GH_REPO}/actions/workflows/${w.wf}/runs?per_page=1`,
        { headers });
      if (!r.ok) continue;
      const j = await r.json();
      const last = j.workflow_runs && j.workflow_runs[0];
      const ageMin = last
        ? (Date.now() - new Date(last.created_at).getTime()) / 60000
        : Infinity;
      if (ageMin > w.maxAgeMin) {
        const d = await fetch(
          `https://api.github.com/repos/${GH_REPO}/actions/workflows/${w.wf}/dispatches`,
          { method: 'POST', headers, body: JSON.stringify({ ref: 'main' }) });
        if (d.status === 204) kicked.push(`${w.wf} (fermo da ${Math.round(ageMin)} min)`);
      }
    } catch (e) { /* singolo workflow: best-effort, si riprova al giro dopo */ }
  }
  if (kicked.length && env.TG_BOT_TOKEN && env.TG_CHAT_ID) {
    try {
      await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: env.TG_CHAT_ID,
          text: '🔁 Watchdog Cloudflare: riavviati workflow fermi:\n' +
                kicked.map(k => `• ${k}`).join('\n'),
        }),
      });
    } catch (e) { /* notifica opzionale */ }
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(watchdog(env));
  },

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
