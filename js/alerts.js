/**
 * alerts.js — Sistema di allerta soglie precipitazioni
 * Controlla i valori estratti vs soglie configurabili.
 * Emette notifiche visive nel pannello e (se permesso) browser notifications.
 */

const AlertSystem = (() => {
  let _enabled = false;
  let _customThresholds = {}; // override rispetto a CONFIG.ALERT_THRESHOLDS
  let _alertLog = [];         // storico allerte
  let _notifPermission = false;
  // Soglie OPERATIVE per area preset, da archive/areas.json (monitoring.products.<P>.thresholds):
  // { areaName: { SRT1: [{level, value}], CUM3: [...] } } — ordinate per valore crescente.
  let _areaThr = {};
  const _LEVEL_UI = { warning: 'warn', alarm: 'danger', emergency: 'danger' };   // → classi CSS esistenti

  let elPanel, elLog, elToggle;

  function init() {
    elPanel  = document.getElementById('alert-panel');
    elLog    = document.getElementById('alert-log');
    elToggle = document.getElementById('alert-toggle');

    elToggle?.addEventListener('change', () => {
      _enabled = elToggle.checked;
      if (_enabled) _requestNotifPermission();
    });

    document.getElementById('btn-clear-alerts')?.addEventListener('click', clearLog);

    // Soglie personalizzate
    document.querySelectorAll('.alert-threshold-input').forEach(inp => {
      inp.addEventListener('change', () => {
        const product = inp.dataset.product;
        const level   = inp.dataset.level; // 'warn' | 'danger'
        if (!_customThresholds[product]) _customThresholds[product] = {};
        _customThresholds[product][level] = parseFloat(inp.value);
      });
    });
  }

  async function _requestNotifPermission() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'granted') { _notifPermission = true; return; }
    const perm = await Notification.requestPermission();
    _notifPermission = perm === 'granted';
  }

  /**
   * Controlla le estrazioni correnti contro le soglie.
   * @param {string} productType
   * @param {number} timestamp
   * @param {{ point, result }[]} extractions
   */
  /**
   * Registra le soglie operative delle aree preset (oggetti `areas` di archive/areas.json).
   * Solo i livelli warning/alarm/emergency con value_mm numerico; nessun valore inventato.
   */
  function setAreaThresholds(areas) {
    _areaThr = {};
    for (const a of areas || []) {
      const prods = a?.monitoring?.products || {};
      const m = {};
      for (const [p, cfg] of Object.entries(prods)) {
        const ths = (cfg?.thresholds || [])
          .filter(t => _LEVEL_UI[t.level] && Number.isFinite(t.value_mm))
          .map(t => ({ level: t.level, value: t.value_mm }))
          .sort((x, y) => x.value - y.value);
        if (ths.length) m[p] = ths;
      }
      if (a?.name) _areaThr[a.name] = m;
    }
  }

  function check(productType, timestamp, extractions) {
    if (!_enabled) return;

    // Soglie indicative locali (config.js) — usate per punti personalizzati
    // e per i prodotti che areas.json non monitora (VMI, SRI, VIL, …).
    const local = {
      ...(CONFIG.ALERT_THRESHOLDS[productType] ?? {}),
      ...(_customThresholds[productType] ?? {}),
    };

    for (const { point, result } of extractions) {
      if (result.mean === null || result.mean === undefined) continue;
      const v = result.mean;

      let level = null, levelLabel = null, thr = null, src, unit;
      const op = point.areaName ? _areaThr[point.areaName]?.[productType] : null;
      if (op && op.length) {
        // Soglie operative dell'area (stesse di monitor.py), valutate sulla media nel buffer
        src = 'soglia areas.json';
        unit = CONFIG.PRODUCTS[productType]?.unit ?? 'mm';
        for (const t of op) if (v >= t.value) { levelLabel = t.level; thr = t.value; }
        if (levelLabel) level = _LEVEL_UI[levelLabel];
        if (levelLabel) levelLabel = ({ warning: 'Liv. 1', alarm: 'Liv. 2', emergency: 'Liv. 3' })[levelLabel] || levelLabel;
      } else {
        if (!local.warn && !local.danger) continue;
        src = 'soglia indicativa locale';
        unit = local.unit;
        if (local.danger !== undefined && v >= local.danger) { level = 'danger'; thr = local.danger; }
        else if (local.warn !== undefined && v >= local.warn) { level = 'warn'; thr = local.warn; }
        levelLabel = level === 'danger' ? 'Liv. 2' : 'Liv. 1';
      }

      if (level) {
        const entry = {
          ts: timestamp,
          point: point.label,
          value: v,
          product: productType,
          unit,
          level,
          levelLabel,
          thr,
          src,
        };
        _addLog(entry);
        _notifyBrowser(entry);
      }
    }
  }

  function _addLog(entry) {
    // Dedup: stessa combinazione ts+point+level negli ultimi 30 min
    const recent = _alertLog.find(a =>
      a.point === entry.point &&
      a.level === entry.level &&
      a.levelLabel === entry.levelLabel &&
      a.product === entry.product &&
      Math.abs(a.ts - entry.ts) < 30 * 60_000
    );
    if (recent) return;

    _alertLog.unshift(entry);
    if (_alertLog.length > 50) _alertLog.pop();
    _renderLog();

    // Badge sul pannello
    const badge = document.getElementById('alert-badge');
    if (badge) {
      const warnCount = _alertLog.filter(a => a.level === 'warn').length;
      const dangerCount = _alertLog.filter(a => a.level === 'danger').length;
      badge.textContent = _alertLog.length;
      badge.className = 'alert-badge ' + (dangerCount > 0 ? 'danger' : 'warn');
      badge.style.display = '';
    }
  }

  function _notifyBrowser(entry) {
    if (!_notifPermission) return;
    const title = `Radar DPC — ${entry.levelLabel || 'soglia'}`;
    const body  = `${entry.point}: ${entry.product} = ${entry.value.toFixed(1)} ${entry.unit} (${entry.levelLabel}, ${entry.src}) — indicativo`;
    try {
      new Notification(title, { body, icon: 'icons/favicon-32x32.png' });
    } catch {}
  }

  function _renderLog() {
    if (!elLog) return;
    if (!_alertLog.length) {
      elLog.innerHTML = '<p class="no-alerts">Nessuna allerta attiva</p>';
      return;
    }
    elLog.innerHTML = _alertLog.map(a => {
      const d = new Date(a.ts);
      const time = Timezone.format(d.getTime(), { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' });
      return `
        <div class="alert-item ${a.level}">
          <span class="alert-icon">${a.level === 'danger' ? '🚨' : '⚠️'}</span>
          <div class="alert-body">
            <strong>${a.point}</strong>
            <span>${a.product} = ${a.value.toFixed(1)} ${a.unit ?? ''} · ${a.levelLabel ?? a.level} ≥ ${a.thr ?? '—'}</span>
            <span style="font-size:10px;color:var(--text3)">${a.src ?? ''}</span>
          </div>
          <span class="alert-time">${time} ${Timezone.suffix(d.getTime())}</span>
        </div>
      `;
    }).join('');
  }

  function clearLog() {
    _alertLog = [];
    _renderLog();
    const badge = document.getElementById('alert-badge');
    if (badge) badge.style.display = 'none';
  }

  function setEnabled(v) { _enabled = v; if (elToggle) elToggle.checked = v; }
  function refresh() {
    if (typeof _renderLog === 'function') { try { _renderLog(); } catch (_) {} }
  }


  return { refresh, init, check, clearLog, setEnabled, setAreaThresholds };
})();


// Re-render lista allerte al cambio fuso orario
window.addEventListener('timezone-changed', () => {
  try { AlertSystem?.refresh?.(); } catch (_) {}
});
