// Scheduler radar-dpc — lancia i workflow GitHub quando il cron di GitHub è in ritardo.
// Cron Trigger: */5 * * * *
// Variabili richieste (Settings → Variables and Secrets, tipo Secret):
//   GH_TOKEN, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, MANUAL_KEY

const REPO = "EbbeneTriglav/radar-dpc";
const BRANCH = "main";

// every    = ogni quanti minuti deve girare (allineato ai cron reali del repo)
// critical = oltre quanti minuti di fermo mandare un Telegram
const JOBS = [
  { file: "arpa-collect.yml",    every: 10,   critical: 180 },
  { file: "monitor.yml",         every: 15,   critical: 180 },
  { file: "nowcast.yml",         every: 10,   critical: 240 },   // 10' (latenza allerte, ott-2026)
  { file: "forecast-alert.yml",  every: 120,  critical: 480 },
  { file: "archive-daily.yml",   every: 360,  critical: 900 },
  { file: "forecast-verify.yml", every: 1440, critical: 2160 },
];

async function gh(path, env, init = {}) {
  return fetch("https://api.github.com" + path, {
    ...init,
    headers: {
      "Authorization": "Bearer " + env.GH_TOKEN,
      "Accept": "application/vnd.github+json",
      "User-Agent": "radar-dpc-scheduler",
      ...(init.headers || {}),
    },
  });
}

async function tg(text, env) {
  if (!env.TELEGRAM_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  await fetch("https://api.telegram.org/bot" + env.TELEGRAM_TOKEN + "/sendMessage", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
}

async function tick(env) {
  const problems = [];
  const log = [];
  for (const job of JOBS) {
    try {
      const r = await gh(`/repos/${REPO}/actions/workflows/${job.file}/runs?per_page=1`, env);
      if (!r.ok) { problems.push(`${job.file}: API GitHub ${r.status}`); continue; }
      const runs = (await r.json()).workflow_runs || [];
      const ageMin = runs.length ? (Date.now() - new Date(runs[0].created_at)) / 60000 : 99999;
      if (ageMin < job.every - 2) { log.push(`${job.file}: ok (${Math.round(ageMin)} min)`); continue; }
      // GitHub a volte risponde 5xx al dispatch (errore temporaneo lato GitHub): fino a 3 tentativi.
      let d = null;
      for (let k = 0; k < 3; k++) {
        d = await gh(`/repos/${REPO}/actions/workflows/${job.file}/dispatches`, env, {
          method: "POST",
          body: JSON.stringify({ ref: BRANCH }),
        });
        if (d.ok || d.status < 500) break;
        await new Promise(res => setTimeout(res, 3000 * (k + 1)));
      }
      // Telegram solo se serve davvero: workflow in ritardo oltre 2 intervalli, oppure errore 4xx
      // (token o configurazione, non si risolve da solo). Un 500 isolato mentre il cron GitHub
      // gira lo stesso non è un problema: lo scheduler riprova tra 5'.
      const late = ageMin > 2 * job.every + 5;
      if (!d.ok && (late || d.status < 500)) problems.push(`${job.file}: avvio FALLITO (${d.status}), fermo da ${Math.round(ageMin)} min`);
      else if (d.ok && ageMin > job.critical) problems.push(`${job.file}: fermo da ${Math.round(ageMin)} min, riavviato`);
      log.push(`${job.file}: ${d.ok ? "riavviato" : "avvio fallito " + d.status} (fermo da ${Math.round(ageMin)} min)`);
    } catch (e) {
      problems.push(`${job.file}: ${e.message}`);
    }
  }
  if (problems.length) await tg("⚠️ Scheduler radar-dpc:\n• " + problems.join("\n• "), env);
  return log;
}

export default {
  async scheduled(event, env, ctx) { ctx.waitUntil(tick(env)); },
  async fetch(req, env) {
    const key = new URL(req.url).searchParams.get("key");
    if (env.MANUAL_KEY && key === env.MANUAL_KEY) {
      const log = await tick(env);
      return new Response(log.join("\n"), { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return new Response("ok");
  },
};
