/**
 * storico.js — Logica pagina storico dati
 * Download CUM24 giornaliero per range date e punti selezionati.
 */

const StoricoApp = (() => {
  let _map = null;
  let _points = [];
  let _isRunning = false;

  let elDateFrom, elDateTo, elProduct, elRun, elProgress, elProgressBar,
      elProgressText, elResultTable, elDownloadBtn, elLog;

  function init() {
    _map = L.map('storico-map', {
      center: CONFIG.MAP.CENTER,
      zoom: 6,
      zoomControl: true,
    });
    L.tileLayer(CONFIG.MAP.TILE_URL, {
      attribution: CONFIG.MAP.TILE_ATTR,
      subdomains: 'abcd',
    }).addTo(_map);

    GeoRasterUtils.init(_map);
    LocationPanel.init(_map, (pts) => { _points = pts; });

    elDateFrom     = document.getElementById('date-from');
    elDateTo       = document.getElementById('date-to');
    elProduct      = document.getElementById('product-select');
    elRun          = document.getElementById('btn-run');
    elProgress     = document.getElementById('progress-wrap');
    elProgressBar  = document.getElementById('progress-bar');
    elProgressText = document.getElementById('progress-text');
    elResultTable  = document.getElementById('result-table');
    elDownloadBtn  = document.getElementById('btn-download');
    elLog          = document.getElementById('run-log');

    // Default: ultime 7 giorni
    const today = new Date();
    const week  = new Date(today - 7 * 86400_000);
    if (elDateTo)   elDateTo.value   = _toInputDate(today);
    if (elDateFrom) elDateFrom.value = _toInputDate(week);

    // Popola prodotti selezionabili (quelli con step ≥ 1h)
    if (elProduct) {
      const cumProds = Object.entries(CONFIG.PRODUCTS)
        .filter(([, p]) => p.category === 'cumulate');
      elProduct.innerHTML = cumProds.map(([type, p]) =>
        `<option value="${type}">${p.label}</option>`
      ).join('');
    }

    elRun?.addEventListener('click', run);
    elDownloadBtn?.addEventListener('click', downloadCSV);
  }

  let _results = []; // [{ date, ...pointId: value }]

  async function run() {
    if (_isRunning) return;
    if (!_points.length) { showToast('Aggiungi almeno un punto dalla mappa', 'warn'); return; }

    const from = new Date(elDateFrom.value + 'T00:00:00Z');
    const to   = new Date(elDateTo.value   + 'T23:59:59Z');
    if (isNaN(from) || isNaN(to) || from > to) {
      showToast('Range date non valido', 'error'); return;
    }

    const productType = elProduct.value;
    const prod = CONFIG.PRODUCTS[productType];
    const days = Math.round((to - from) / 86400_000) + 1;
    if (days > 90) { showToast('Range massimo 90 giorni', 'warn'); return; }

    _isRunning = true;
    _results = [];
    elRun.disabled = true;
    elRun.innerHTML = '<i class="fa fa-circle-notch fa-spin"></i> Esecuzione…';
    elProgress.style.display = '';
    elLog.innerHTML = '';

    let processed = 0;

    for (let d = 0; d < days; d++) {
      if (!_isRunning) break;
      const dayMs = from.getTime() + d * 86400_000;
      const dayStr = new Date(dayMs).toISOString().slice(0, 10);

      _log(`📅 ${dayStr} — Richiesta ${productType}…`);
      setProgress(d, days, `Giorno ${d + 1}/${days}: ${dayStr}`);

      try {
        // Prendi l'ultimo prodotto disponibile per quel giorno
        // L'API non ha filtro data storica diretta → usiamo mezzanotte del giorno come ts
        const noonTs = dayMs + 12 * 3600_000; // mezzogiorno del giorno

        const { url } = await RadarAPI.getDownloadUrl(productType, noonTs);
        const buffer = await RadarAPI.fetchGeoTiff(url);
        const georaster = await GeoRasterUtils.parseGeoTiff(buffer);

        const row = { date: dayStr };
        for (const point of _points) {
          const res = GeoRasterUtils.extractBuffer(georaster, point.lat, point.lon);
          row[point.id] = res.mean;
          row[`${point.id}_min`] = res.min;
          row[`${point.id}_max`] = res.max;
        }
        _results.push(row);
        _log(`  ✅ OK — ${_points.map(p => `${p.label}: ${row[p.id]?.toFixed(1) ?? 'N/D'} ${prod.unit}`).join(' | ')}`);

      } catch (e) {
        _log(`  ⚠️ ${dayStr}: ${e.message}`);
        _results.push({ date: dayStr, error: e.message });
      }

      processed++;
      // Rispetta il rate limit (max ~1 req/s per S3)
      await _sleep(1100);
    }

    setProgress(days, days, 'Completato');
    _renderTable(productType);
    elDownloadBtn.style.display = '';
    const xlsxBtn = document.getElementById('btn-download-xlsx');
    if (xlsxBtn) xlsxBtn.style.display = '';
    _isRunning = false;
    elRun.disabled = false;
    elRun.innerHTML = '<i class="fa fa-play"></i> Avvia';
    showToast(`Elaborazione completata: ${processed} giorni`, 'success');
  }

  function _renderTable(productType) {
    if (!elResultTable || !_results.length) return;
    const prod = CONFIG.PRODUCTS[productType];
    const header = ['Data', ..._points.map(p => p.label + ' (' + prod.unit + ')')].join('</th><th>');
    const rows = _results.map(row => {
      const cells = [row.date, ..._points.map(p => {
        const v = row[p.id];
        return v !== undefined && v !== null ? v.toFixed(2) : (row.error ? '⚠️' : 'N/D');
      })].join('</td><td>');
      return `<tr><td>${cells}</td></tr>`;
    }).join('');
    elResultTable.innerHTML = `<table class="result-table"><thead><tr><th>${header}</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function downloadCSV() {
    if (!_results.length) return;
    const prodType = elProduct.value;
    const prod = CONFIG.PRODUCTS[prodType];
    const header = ['Data', ..._points.map(p => `${p.label}_${prod.unit}`)].join(',');
    const rows = _results.map(row =>
      [row.date, ..._points.map(p => row[p.id]?.toFixed(3) ?? '')].join(',')
    );
    const csv = [header, ...rows].join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = `storico_${prodType}_${elDateFrom.value}_${elDateTo.value}.csv`;
    a.click();
  }

  /** Export Excel con SheetJS — include un foglio per ogni punto */
  function downloadExcel() {
    if (!_results.length) { showToast('Nessun dato da esportare', 'warn'); return; }
    if (typeof XLSX === 'undefined') { showToast('SheetJS non disponibile', 'error'); return; }

    const prodType = elProduct.value;
    const prod = CONFIG.PRODUCTS[prodType];
    const unit = prod?.unit ?? '';
    const wb = XLSX.utils.book_new();

    // ─── Foglio riepilogo (tutti i punti) ────────────────────────────────
    const summaryRows = _results.map(row => {
      const obj = { Data: row.date };
      _points.forEach(p => {
        obj[`${p.label} – Media (${unit})`] = row[p.id] !== undefined && row[p.id] !== null
          ? +row[p.id].toFixed(3) : null;
        obj[`${p.label} – Min (${unit})`]   = row[`${p.id}_min`]?.toFixed(3) != null
          ? +row[`${p.id}_min`].toFixed(3) : null;
        obj[`${p.label} – Max (${unit})`]   = row[`${p.id}_max`]?.toFixed(3) != null
          ? +row[`${p.id}_max`].toFixed(3) : null;
        if (row.error) obj['Errore'] = row.error;
      });
      return obj;
    });
    const wsSummary = XLSX.utils.json_to_sheet(summaryRows);
    XLSX.utils.book_append_sheet(wb, wsSummary, 'Riepilogo');

    // ─── Un foglio per ogni punto ─────────────────────────────────────────
    _points.forEach(p => {
      const rows = _results.map(row => ({
        Data:                  row.date,
        [`Media (${unit})`]:   row[p.id] !== undefined && row[p.id] !== null ? +row[p.id].toFixed(3) : null,
        [`Min (${unit})`]:     row[`${p.id}_min`]?.toFixed(3) != null ? +row[`${p.id}_min`].toFixed(3) : null,
        [`Max (${unit})`]:     row[`${p.id}_max`]?.toFixed(3) != null ? +row[`${p.id}_max`].toFixed(3) : null,
        Errore:                row.error ?? '',
      }));
      const ws = XLSX.utils.json_to_sheet(rows);
      ws['!cols'] = [{ wch: 12 }, { wch: 14 }, { wch: 12 }, { wch: 12 }, { wch: 30 }];
      // Label sicura per nome foglio (max 31 char, no special chars)
      const sheetName = p.label.replace(/[\\\/\?\*\[\]:]/g, '').slice(0, 28) || `Punto ${p.id}`;
      XLSX.utils.book_append_sheet(wb, ws, sheetName);
    });

    // ─── Foglio metadati ──────────────────────────────────────────────────
    const meta = [
      { Campo: 'Prodotto',   Valore: prodType },
      { Campo: 'Unità',      Valore: unit },
      { Campo: 'Da',         Valore: elDateFrom.value },
      { Campo: 'A',          Valore: elDateTo.value },
      { Campo: 'Buffer km',  Valore: CONFIG.BUFFER_KM },
      { Campo: 'Generato',   Valore: new Date().toISOString() },
      { Campo: 'Fonte',      Valore: 'Radar DPC — Protezione Civile Italiana' },
      { Campo: 'API',        Valore: 'https://radar-api.protezionecivile.it' },
      ..._points.map((p, i) => ({
        Campo: `Punto ${i + 1}`,
        Valore: `${p.label} (${p.lat.toFixed(5)}, ${p.lon.toFixed(5)})`,
      })),
    ];
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(meta), 'Metadati');

    const filename = `storico_${prodType}_${elDateFrom.value}_${elDateTo.value}.xlsx`;
    XLSX.writeFile(wb, filename);
    showToast('Excel scaricato ✅', 'success', 2500);
  }

  function setProgress(done, total, msg) {
    if (!elProgressBar) return;
    const pct = total ? Math.round(done / total * 100) : 0;
    elProgressBar.style.width = pct + '%';
    if (elProgressText) elProgressText.textContent = msg || pct + '%';
  }

  function _log(msg) {
    if (!elLog) return;
    const line = document.createElement('div');
    line.className = 'log-line';
    line.textContent = msg;
    elLog.appendChild(line);
    elLog.scrollTop = elLog.scrollHeight;
  }

  function _toInputDate(date) {
    return date.toISOString().slice(0, 10);
  }

  function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  return { init, downloadCSV, downloadExcel };
})();

function showToast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.classList.add('show'), 10);
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 4000);
}

document.addEventListener('DOMContentLoaded', () => StoricoApp.init());
