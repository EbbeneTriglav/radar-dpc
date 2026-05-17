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
  function check(productType, timestamp, extractions) {
    if (!_enabled) return;

    const thresholds = {
      ...(CONFIG.ALERT_THRESHOLDS[productType] ?? {}),
      ...(_customThresholds[productType] ?? {}),
    };
    if (!thresholds.warn && !thresholds.danger) return;

    for (const { point, result } of extractions) {
      if (result.mean === null) continue;
      const v = result.mean;

      let level = null;
      if (thresholds.danger !== undefined && v >= thresholds.danger) level = 'danger';
      else if (thresholds.warn !== undefined && v >= thresholds.warn) level = 'warn';

      if (level) {
        const entry = {
          ts: timestamp,
          point: point.label,
          value: v,
          product: productType,
          unit: thresholds.unit,
          level,
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
    const icon = entry.level === 'danger' ? '🚨' : '⚠️';
    const title = `${icon} Radar DPC — ${entry.level === 'danger' ? 'PERICOLO' : 'Attenzione'}`;
    const body  = `${entry.point}: ${entry.product} = ${entry.value.toFixed(1)} ${entry.unit}`;
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
      const time = d.toLocaleString('it-IT', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', timeZone:'UTC' });
      return `
        <div class="alert-item ${a.level}">
          <span class="alert-icon">${a.level === 'danger' ? '🚨' : '⚠️'}</span>
          <div class="alert-body">
            <strong>${a.point}</strong>
            <span>${a.product} = ${a.value.toFixed(1)} ${a.unit}</span>
          </div>
          <span class="alert-time">${time} UTC</span>
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

  return { init, check, clearLog, setEnabled };
})();
