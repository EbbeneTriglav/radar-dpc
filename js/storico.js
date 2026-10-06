/**
 * storico.js — Logica pagina storico dati
 * Download cumulate DPC (CUM24 = giorno UTC intero) per range date e punti selezionati.
 * NB: CUM3/6/12/24 DPC sono pluviometri DPC interpolati (non radar).
 *
 * Finestre temporali (prodotto DPC datato T = cumulata delle N ore che TERMINANO a T):
 *   - CUM24: si richiede il prodotto delle 00:00 UTC di D+1 → copre esattamente il giorno UTC D.
 *   - CUM3/CUM6/CUM12: si richiede UN solo blocco, quello che termina alle 12:00 UTC di D
 *     (NON è il totale del giorno; sommare tutti i blocchi richiederebbe 2–8 richieste/giorno).
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

    // Layer picker: 5 basemap free
    BasemapPicker.init(_map);

    GeoRasterUtils.init(_map);
    LocationPanel.init(_map, (pts) => { _points = pts; });

    elDateFrom     = document.getElementById('date-from');
    elDateTo       = document.getElementById('date-to');
    elProduct      = document.getElementById('product-select');
    elRun          = document.getElementById('btn-run');
    elProgress     = document.getElementById('progress-wrap');
    elProgressBar  = document.getElementById('progress-bar');
    elProgressText = document.getElementById('progress-text');
    elResultTable  = document.getElementById('storico-results-inner');
    elDownloadBtn  = document.getElementById('btn-download');
    elLog          = document.getElementById('run-log');

    // Default: ultimi 7 giorni CONCLUSI (fino a ieri UTC: il giorno in corso non ha ancora la CUM24)
    const yesterday = new Date(Date.now() - 86400_000);
    const weekAgo   = new Date(yesterday.getTime() - 6 * 86400_000);
    if (elDateTo)   elDateTo.value   = _toInputDate(yesterday);
    if (elDateFrom) elDateFrom.value = _toInputDate(weekAgo);

    // Popola prodotti selezionabili: CUM24 per primo (default) = giorno UTC intero;
    // gli altri sono un singolo blocco che termina alle 12 UTC (dichiarato nell'etichetta).
    if (elProduct) {
      const cumProds = Object.entries(CONFIG.PRODUCTS)
        .filter(([, p]) => p.category === 'cumulate')
        .sort(([a], [b]) => (a === 'CUM24' ? -1 : b === 'CUM24' ? 1 : 0));
      elProduct.innerHTML = cumProds.map(([type, p]) =>
        `<option value="${type}"${type === 'CUM24' ? ' selected' : ''}>${p.label} — ${_windowLabel(type)}</option>`
      ).join('');
    }

    elRun?.addEventListener('click', run);
    // Il download CSV è collegato via onclick nell'HTML (un solo handler → un solo file).
  }

  /** Descrizione della finestra temporale del valore giornaliero per il prodotto. */
  function _windowLabel(type) {
    return type === 'CUM24'
      ? 'giorno UTC intero (00→24 UTC)'
      : 'blocco che termina alle 12 UTC (non il giorno intero)';
  }

  /** Istante (ms) del prodotto da richiedere per il giorno UTC che inizia a dayMs. */
  function _productTs(type, dayMs) {
    return type === 'CUM24' ? dayMs + 86400_000 : dayMs + 12 * 3600_000;
  }

  let _results = []; // [{ date, ...pointId: value }]

  async function run() {
    if (_isRunning) return;
    if (!_points.length) { showToast('Aggiungi almeno un punto dalla mappa', 'warn'); return; }

    const from = new Date(elDateFrom.value + 'T00:00:00Z');
    const toDay = Date.parse(elDateTo.value + 'T00:00:00Z');
    if (isNaN(from) || isNaN(toDay) || from.getTime() > toDay) {
      showToast('Range date non valido', 'error'); return;
    }

    const productType = elProduct.value;
    const prod = CONFIG.PRODUCTS[productType];
    const days = Math.round((toDay - from.getTime()) / 86400_000) + 1;
    if (days > 90) { showToast('Range massimo 90 giorni', 'warn'); return; }

    _isRunning = true;
    _results = [];
    elRun.disabled = true;
    elRun.innerHTML = '<i class="fa fa-circle-notch fa-spin"></i> Esecuzione…';
    elProgress.style.display = 'block';  // style.css nasconde #progress-wrap di default
    elLog.innerHTML = '';

    let processed = 0;

    for (let d = 0; d < days; d++) {
      if (!_isRunning) break;
      const dayMs = from.getTime() + d * 86400_000;
      const dayStr = new Date(dayMs).toISOString().slice(0, 10);

      setProgress(d, days, `Giorno ${d + 1}/${days}: ${dayStr}`);

      // Mai richiedere prodotti futuri: il giorno non è ancora concluso → dato mancante.
      const prodTs = _productTs(productType, dayMs);
      if (prodTs <= Date.now()) _log(`📅 ${dayStr} — Richiesta ${productType}…`);
      if (prodTs > Date.now()) {
        const why = 'prodotto non ancora disponibile (finestra non conclusa)';
        _log(`  ⏳ ${dayStr}: ${why}`);
        _results.push({ date: dayStr, error: why });
        processed++;
        continue;
      }

      try {
        const { url } = await RadarAPI.getDownloadUrl(productType, prodTs);
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
        _log(`  ✅ OK — ${_points.map(p => `${p.label}: ${row[p.id]?.toFixed(1) ?? '—'} ${prod.unit}`).join(' | ')}`);

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
    const nMiss = _results.filter(r => r.error).length;
    showToast(`Elaborazione completata: ${processed} giorni` +
      (nMiss ? ` (${nMiss} senza dato, mostrati come —)` : ''), nMiss ? 'warn' : 'success');
  }

  function _renderTable(productType) {
    if (!elResultTable || !_results.length) return;
    const prod = CONFIG.PRODUCTS[productType];
    const esc = t => String(t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const dateHdr = productType === 'CUM24' ? 'Giorno UTC' : 'Giorno (blocco fino alle 12 UTC)';
    const header = [dateHdr, ..._points.map(p => esc(p.label) + ' (' + prod.unit + ')')].join('</th><th>');
    const rows = _results.map(row => {
      // Dato mancante = "—" (mai 0); il motivo è nel tooltip
      const miss = `<span title="${esc(row.error || 'nessun valore nel buffer')}">—</span>`;
      const cells = [row.date, ..._points.map(p => {
        const v = row[p.id];
        return v !== undefined && v !== null ? v.toFixed(2) : miss;
      })].join('</td><td>');
      return `<tr><td>${cells}</td></tr>`;
    }).join('');
    const note = `<p style="font-size:10px;color:var(--text3);margin:0 0 6px">${productType} = pluviometri DPC interpolati (non radar) — ${_windowLabel(productType)}. Media nel buffer di ${CONFIG.BUFFER_KM} km. "—" = dato mancante.</p>`;
    elResultTable.innerHTML = `${note}<table class="result-table"><thead><tr><th>${header}</th></tr></thead><tbody>${rows}</tbody></table>`;
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
      { Campo: 'Finestra',   Valore: _windowLabel(prodType) +
          (prodType === 'CUM24' ? ' (prodotto delle 00:00 UTC del giorno successivo)' : ' (un solo blocco al giorno)') },
      { Campo: 'Unità',      Valore: unit },
      { Campo: 'Da',         Valore: elDateFrom.value },
      { Campo: 'A',          Valore: elDateTo.value },
      { Campo: 'Buffer km',  Valore: CONFIG.BUFFER_KM },
      { Campo: 'Generato',   Valore: new Date().toISOString() },
      { Campo: 'Fonte',      Valore: 'DPC Protezione Civile — cumulate CUM = pluviometri DPC interpolati (non radar)' },
      { Campo: 'Dati mancanti', Valore: 'celle vuote (nessun valore inventato)' },
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
