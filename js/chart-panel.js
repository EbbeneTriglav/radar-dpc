/**
 * chart-panel.js — Pannello grafico serie temporale (Chart.js)
 * Gestisce la visualizzazione dei valori estratti per i punti selezionati.
 */

const ChartPanel = (() => {
  let _chart = null;
  let _history = {}; // { pointId: [{ ts, mean, min, max }] }
  let _productType = 'VMI';
  let _maxHistoryPoints = 48;

  let elPanel, elCanvas, elEmpty, elUnit;

  function init() {
    elPanel  = document.getElementById('chart-panel');
    elCanvas = document.getElementById('chart-canvas');
    elEmpty  = document.getElementById('chart-empty');
    elUnit   = document.getElementById('chart-unit');

    document.getElementById('btn-clear-chart')?.addEventListener('click', clearHistory);
    document.getElementById('btn-download-chart')?.addEventListener('click', downloadCSV);
  }

  function setProduct(productType) {
    _productType = productType;
    clearHistory();
  }

  /**
   * Aggiunge un record per ogni punto al timestamp corrente.
   * @param {number} timestamp  - epoch ms
   * @param {{ point, result }[]} extractions
   */
  function addData(timestamp, extractions) {
    const prod = CONFIG.PRODUCTS[_productType];
    if (elUnit) elUnit.textContent = prod?.unit ?? '';

    let changed = false;
    for (const { point, result } of extractions) {
      if (!_history[point.id]) _history[point.id] = [];
      const arr = _history[point.id];

      // Evita duplicati
      if (arr.length && arr[arr.length - 1].ts === timestamp) continue;

      arr.push({ ts: timestamp, ...result });
      if (arr.length > _maxHistoryPoints) arr.shift();
      changed = true;
    }

    // Rimuovi storia di punti eliminati
    const activeIds = new Set(extractions.map(e => e.point.id));
    Object.keys(_history).forEach(id => {
      if (!activeIds.has(parseInt(id))) delete _history[id];
    });

    if (changed || !_chart) _renderChart(extractions.map(e => e.point));
  }

  function _renderChart(points) {
    if (!elCanvas) return;

    if (!points.length || Object.keys(_history).length === 0) {
      if (elEmpty) elEmpty.style.display = 'flex';
      if (_chart) { _chart.destroy(); _chart = null; }
      return;
    }
    if (elEmpty) elEmpty.style.display = 'none';

    // Unione di tutti i timestamp ordinati
    const allTs = [...new Set(
      Object.values(_history).flatMap(arr => arr.map(d => d.ts))
    )].sort((a, b) => a - b);

    const labels = allTs.map(ts => {
      const d = new Date(ts);
      return Timezone.formatShort(d.getTime());
    });

    const datasets = points.map(point => {
      const hist = _history[point.id] ?? [];
      const tsMap = Object.fromEntries(hist.map(d => [d.ts, d]));

      return {
        label: point.label,
        data: allTs.map(ts => {
          const d = tsMap[ts];
          return d?.mean !== null ? +(d.mean.toFixed(2)) : null;
        }),
        borderColor: point.color,
        backgroundColor: point.color + '22',
        borderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 5,
        tension: 0.3,
        spanGaps: true,
        fill: false,
      };
    });

    const prod = CONFIG.PRODUCTS[_productType];
    const unit = prod?.unit ?? '';

    if (_chart) {
      _chart.data.labels = labels;
      _chart.data.datasets = datasets;
      _chart.update('none');
      return;
    }

    const ctx = elCanvas.getContext('2d');
    _chart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 200 },
        plugins: {
          legend: {
            labels: { color: '#cdd6f4', font: { family: 'IBM Plex Mono', size: 11 } },
          },
          tooltip: {
            backgroundColor: '#1e1e2e',
            titleColor: '#cdd6f4',
            bodyColor: '#a6adc8',
            borderColor: '#45475a',
            borderWidth: 1,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(2) ?? 'N/D'} ${unit}`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#6c7086', font: { size: 10, family: 'IBM Plex Mono' }, maxTicksLimit: 8 },
            grid: { color: '#313244' },
          },
          y: {
            ticks: { color: '#6c7086', font: { size: 10, family: 'IBM Plex Mono' } },
            grid: { color: '#313244' },
            title: {
              display: !!unit,
              text: unit,
              color: '#585b70',
              font: { size: 11 },
            },
          },
        },
      },
    });
  }

  function clearHistory() {
    _history = {};
    if (_chart) { _chart.destroy(); _chart = null; }
    if (elEmpty) elEmpty.style.display = 'flex';
  }

  /** Esporta i dati in CSV */
  function downloadCSV() {
    const prod = CONFIG.PRODUCTS[_productType];
    const unit = prod?.unit ?? '';
    const rows = [['Timestamp_UTC', 'Punto', 'Media_' + unit, 'Min_' + unit, 'Max_' + unit, 'Pixel_count']];

    Object.entries(_history).forEach(([id, arr]) => {
      arr.forEach(d => {
        rows.push([
          new Date(d.ts).toISOString(),
          id,
          d.mean?.toFixed(3) ?? '',
          d.min?.toFixed(3) ?? '',
          d.max?.toFixed(3) ?? '',
          d.count ?? '',
        ]);
      });
    });

    const csv = rows.map(r => r.join(',')).join('\n');
    _downloadBlob(csv, `radar_${_productType}_${Date.now()}.csv`, 'text/csv');
  }

  /** Esporta i dati in Excel (.xlsx) tramite SheetJS */
  function downloadExcel() {
    if (typeof XLSX === 'undefined') {
      showToast('Libreria SheetJS non disponibile', 'error'); return;
    }
    const prod = CONFIG.PRODUCTS[_productType];
    const unit = prod?.unit ?? '';

    // Costruisci array di oggetti per SheetJS
    const rows = [];
    Object.entries(_history).forEach(([id, arr]) => {
      arr.forEach(d => {
        rows.push({
          'Timestamp UTC':      new Date(d.ts).toISOString(),
          'Punto ID':           id,
          [`Media (${unit})`]:  d.mean !== null ? +d.mean.toFixed(3) : null,
          [`Min (${unit})`]:    d.min  !== null ? +d.min.toFixed(3)  : null,
          [`Max (${unit})`]:    d.max  !== null ? +d.max.toFixed(3)  : null,
          'N° pixel':           d.count ?? 0,
        });
      });
    });

    const wb = XLSX.utils.book_new();
    const ws = XLSX.utils.json_to_sheet(rows);

    // Larghezze colonne
    ws['!cols'] = [
      { wch: 24 }, { wch: 12 }, { wch: 14 }, { wch: 14 }, { wch: 14 }, { wch: 10 }
    ];

    // Foglio info prodotto
    const infoRows = [
      { Campo: 'Prodotto',   Valore: _productType },
      { Campo: 'Unità',      Valore: unit },
      { Campo: 'Buffer km',  Valore: CONFIG.BUFFER_KM },
      { Campo: 'Generato',   Valore: new Date().toISOString() },
      { Campo: 'Fonte dati', Valore: 'Radar DPC — Protezione Civile Italiana' },
    ];
    const wsInfo = XLSX.utils.json_to_sheet(infoRows);

    XLSX.utils.book_append_sheet(wb, ws,     'Dati radar');
    XLSX.utils.book_append_sheet(wb, wsInfo, 'Info');

    XLSX.writeFile(wb, `radar_${_productType}_${Date.now()}.xlsx`);
    showToast('Excel scaricato ✅', 'success', 2500);
  }

  function _downloadBlob(content, filename, mime) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([content], { type: mime }));
    a.download = filename;
    a.click();
  }

  function getHistory() { return JSON.parse(JSON.stringify(_history)); }
  function refresh() {
    if (typeof _chart !== 'undefined' && _chart) {
      try { _chart.update(); } catch (_) {}
    }
  }


  return { refresh, init, setProduct, addData, clearHistory, downloadCSV, downloadExcel, getHistory };
})();


// Refresh tick X-axis al cambio fuso orario
window.addEventListener('timezone-changed', () => {
  try { ChartPanel?.refresh?.(); } catch (_) {}
});
