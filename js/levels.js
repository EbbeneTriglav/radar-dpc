/**
 * levels.js — etichette NEUTRE dei livelli di allerta per le pagine pubbliche.
 *
 * I file dati (events.csv, episodes_alerts.csv, ...) conservano i codici
 * originali (warning/alarm/emergency, nowcast_*, forecast24h_*, ...): servono
 * agli script e ai report interni. Sul sito pubblico si mostrano solo
 * "Liv. 1/2/3" con colori smorzati, senza nomi che ne enfatizzino la gravita'.
 */
(function () {
  const NUM = { warning: 1, alarm: 2, emergency: 3,
                attenzione: 1, critico: 2, estremo: 3 };
  const KIND = [
    ['nowcast_', 'nowcast'], ['forecast24h_', 'previsione 24h'],
    ['forecast_', 'previsione'], ['persistent_', 'pioggia persistente'],
  ];

  function parse(level) {
    const s = String(level || '').trim();
    if (!s) return { n: null, kind: '', text: '—' };
    if (s === 'storm_on_area') return { n: null, kind: 'cella', text: 'cella su area' };
    if (s === 'storm_cleared') return { n: null, kind: 'cella', text: 'chiusura' };
    let kind = '', base = s;
    for (const [p, k] of KIND) if (s.startsWith(p)) { kind = k; base = s.slice(p.length); break; }
    const n = NUM[base.toLowerCase()] ?? null;
    const text = n ? `Liv. ${n}` : base.replace(/_/g, ' ');
    return { n, kind, text };
  }

  // "Liv. 2" (oppure "Liv. 2 · nowcast" con withKind)
  function text(level, withKind) {
    const p = parse(level);
    return withKind && p.kind && p.n ? `${p.text} · ${p.kind}` : p.text;
  }
  // colori smorzati: nessun rosso/arancio, solo intensita' crescente di grigio-blu
  const COLORS = { 1: '#8b9bb0', 2: '#a3b1c2', 3: '#c3ccd8' };
  function color(level) { const p = parse(level); return COLORS[p.n] || '#8b9bb0'; }
  function weight(level) { const p = parse(level); return p.n >= 2 ? 600 : 400; }
  function num(level) { return parse(level).n; }

  window.LVL = { parse, text, color, weight, num, COLORS };
})();
