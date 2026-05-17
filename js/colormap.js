/**
 * colormap.js — Interpolazione scale colori e legenda
 */

const ColorMap = (() => {

  /** Interpola linearmente tra due array RGBA */
  function _lerp(c0, c1, t) {
    return c0.map((ch, i) => Math.round(ch + t * (c1[i] - ch)));
  }

  /**
   * Ritorna [r,g,b,a] per un valore dato e una scala configurata
   * @param {number} value
   * @param {string} scaleName - chiave in CONFIG.COLOR_SCALES
   * @returns {number[]} [r,g,b,a] 0-255
   */
  function getColor(value, scaleName) {
    if (value === null || value === undefined || isNaN(value)) return [0, 0, 0, 0];
    const scale = CONFIG.COLOR_SCALES[scaleName];
    if (!scale) return [128, 128, 128, 120];

    if (value <= scale[0][0]) return [...scale[0][1]];
    if (value >= scale[scale.length - 1][0]) return [...scale[scale.length - 1][1]];

    for (let i = 0; i < scale.length - 1; i++) {
      const [v0, c0] = scale[i];
      const [v1, c1] = scale[i + 1];
      if (value >= v0 && value <= v1) {
        const t = (value - v0) / (v1 - v0);
        return _lerp(c0, c1, t);
      }
    }
    return [0, 0, 0, 0];
  }

  /**
   * Genera i stop per la legenda SVG
   * @param {string} scaleName
   * @returns {{ value: number, color: string }[]}
   */
  function getLegendStops(scaleName) {
    const scale = CONFIG.COLOR_SCALES[scaleName];
    if (!scale) return [];
    return scale.map(([value, [r, g, b, a]]) => ({
      value,
      color: `rgba(${r},${g},${b},${a / 255})`,
    }));
  }

  /**
   * Costruisce la funzione pixelValuesToColorFn per GeoRasterLayer
   */
  function makeColorFn(scaleName, noDataValue) {
    return (values) => {
      const v = values[0];
      if (v === noDataValue || v === null || isNaN(v)) return null;
      const [r, g, b, a] = getColor(v, scaleName);
      if (a < 5) return null;
      return `rgba(${r},${g},${b},${(a / 255).toFixed(2)})`;
    };
  }

  /**
   * Ritorna colore CSS per un valore (utile per UI)
   */
  function toCss(value, scaleName) {
    const [r, g, b, a] = getColor(value, scaleName);
    return `rgba(${r},${g},${b},${(a / 255).toFixed(2)})`;
  }

  /**
   * Genera HTML legenda verticale con gradiente
   */
  function renderLegend(scaleName, unit, containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const stops = getLegendStops(scaleName);
    if (!stops.length) { el.innerHTML = ''; return; }

    // Gradiente CSS
    const gradStops = stops.map((s, i) => {
      const pct = Math.round((i / (stops.length - 1)) * 100);
      return `${s.color} ${pct}%`;
    }).join(', ');

    const labels = stops.map(s => {
      const pct = 100 - Math.round(stops.indexOf(s) / (stops.length - 1) * 100);
      return `<div class="legend-label" style="top:${pct}%">${s.value}</div>`;
    }).join('');

    el.innerHTML = `
      <div class="legend-unit">${unit}</div>
      <div class="legend-bar-wrap">
        <div class="legend-bar" style="background:linear-gradient(to top,${gradStops})"></div>
        <div class="legend-labels">${labels}</div>
      </div>
    `;
  }

  return { getColor, getLegendStops, makeColorFn, toCss, renderLegend };
})();
