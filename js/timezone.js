/**
 * timezone.js — Modulo centralizzato di formattazione orari
 *
 * Permette di alternare tra modalità UTC (come l'API DPC) e Locale
 * (Europe/Rome, gestisce automaticamente CET/CEST in base alla data).
 *
 * La scelta è persistita in localStorage. Quando cambia, viene emesso
 * l'evento `timezone-changed` sul window: i moduli che mostrano orari
 * (player, chart, alerts) lo ascoltano e ridisegnano.
 *
 * API:
 *   Timezone.mode               → 'utc' | 'local'
 *   Timezone.toggle()           → switcha modalità
 *   Timezone.set(mode)          → imposta modalità
 *   Timezone.suffix()           → 'UTC' | 'CEST' | 'CET'
 *   Timezone.format(ms, opts)   → stringa formattata (it-IT)
 *   Timezone.formatTime(ms)     → "11:30 CEST" / "09:30 UTC"
 *   Timezone.formatDateTime(ms) → "18/05/2026 11:30 CEST"
 */

const Timezone = (() => {
  const STORAGE_KEY = 'radar-dpc.timezone';
  const DEFAULT_MODE = 'local';

  let _mode = localStorage.getItem(STORAGE_KEY) || DEFAULT_MODE;
  if (_mode !== 'utc' && _mode !== 'local') _mode = DEFAULT_MODE;

  /** Restituisce il suffisso da mostrare. Per locale calcola CET/CEST. */
  function suffix(ms) {
    if (_mode === 'utc') return 'UTC';
    // Determina se Roma è in CEST (UTC+2) o CET (UTC+1)
    const d = ms != null ? new Date(ms) : new Date();
    // Calcolo offset Roma vs UTC in minuti per la data data
    const utc = new Date(d.toLocaleString('en-US', { timeZone: 'UTC' }));
    const rome = new Date(d.toLocaleString('en-US', { timeZone: 'Europe/Rome' }));
    const offsetMin = (rome - utc) / 60000;
    return offsetMin === 120 ? 'CEST' : 'CET';
  }

  /** Opzioni base per toLocaleString in base alla modalità */
  function _tzOpts() {
    return _mode === 'utc'
      ? { timeZone: 'UTC' }
      : { timeZone: 'Europe/Rome' };
  }

  /** Format generico, prende opzioni Intl.DateTimeFormatOptions */
  function format(ms, opts = {}) {
    return new Date(ms).toLocaleString('it-IT', { ...opts, ..._tzOpts() });
  }

  /** "HH:MM SUFFISSO" — usato nel player */
  function formatTime(ms) {
    const t = new Date(ms).toLocaleTimeString('it-IT', {
      hour: '2-digit', minute: '2-digit', ..._tzOpts(),
    });
    return `${t} ${suffix(ms)}`;
  }

  /** "DD/MM/YYYY HH:MM SUFFISSO" — usato negli alert */
  function formatDateTime(ms) {
    const t = new Date(ms).toLocaleString('it-IT', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', ..._tzOpts(),
    });
    return `${t} ${suffix(ms)}`;
  }

  /** Format breve senza suffisso — usato sull'asse X del grafico */
  function formatShort(ms) {
    return new Date(ms).toLocaleTimeString('it-IT', {
      hour: '2-digit', minute: '2-digit', ..._tzOpts(),
    });
  }

  function set(mode) {
    if (mode !== 'utc' && mode !== 'local') return;
    if (mode === _mode) return;
    _mode = mode;
    localStorage.setItem(STORAGE_KEY, _mode);
    window.dispatchEvent(new CustomEvent('timezone-changed', { detail: { mode: _mode } }));
  }

  function toggle() {
    set(_mode === 'utc' ? 'local' : 'utc');
  }

  return {
    get mode() { return _mode; },
    suffix, format, formatTime, formatDateTime, formatShort, set, toggle,
  };
})();
