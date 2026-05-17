/**
 * player.js — Gestione animazione e controlli temporali
 * Gestisce il caricamento progressivo dei frame e la riproduzione.
 */

const Player = (() => {
  let _timestamps = [];
  let _currentIdx = 0;
  let _isPlaying = false;
  let _playInterval = null;
  let _speedMs = 400;        // ms per frame
  let _productType = 'VMI';
  let _onFrameChange = null; // callback(timestamp, idx, total)
  let _onLoadProgress = null; // callback(loaded, total)
  let _preloaded = new Set();

  // ─── DOM refs ─────────────────────────────────────────────────────────────
  let elSlider, elCurrentTime, elPlay, elPrev, elNext, elSpeed, elFrameCount;

  function init({ onFrameChange, onLoadProgress }) {
    _onFrameChange = onFrameChange;
    _onLoadProgress = onLoadProgress;

    elSlider      = document.getElementById('time-slider');
    elCurrentTime = document.getElementById('current-time');
    elPlay        = document.getElementById('btn-play');
    elPrev        = document.getElementById('btn-prev');
    elNext        = document.getElementById('btn-next');
    elSpeed       = document.getElementById('anim-speed');
    elFrameCount  = document.getElementById('frame-count');

    elPlay?.addEventListener('click', togglePlay);
    elPrev?.addEventListener('click', stepBack);
    elNext?.addEventListener('click', stepForward);
    elSlider?.addEventListener('input', () => {
      _currentIdx = parseInt(elSlider.value);
      _showFrame(_currentIdx);
    });
    elSpeed?.addEventListener('change', () => {
      _speedMs = parseInt(elSpeed.value);
      if (_isPlaying) { _stopPlay(); _startPlay(); }
    });
  }

  /**
   * Carica i timestamp per il prodotto corrente e avvia il pre-fetch
   */
  async function loadProduct(productType, nFrames = CONFIG.MAX_FRAMES) {
    _productType = productType;
    _isPlaying = false;
    _stopPlay();
    _preloaded.clear();
    _currentIdx = 0;

    const prod = CONFIG.PRODUCTS[productType];
    if (!prod || !prod.stepMs) return;

    try {
      const last = await RadarAPI.getLastProduct(productType);
      _timestamps = RadarAPI.buildTimestamps(last.time, prod.stepMs, nFrames);
      _currentIdx = _timestamps.length - 1; // ultimo = più recente

      if (elSlider) {
        elSlider.min = 0;
        elSlider.max = _timestamps.length - 1;
        elSlider.value = _currentIdx;
      }
      if (elFrameCount) elFrameCount.textContent = `${_timestamps.length} frame`;

      _showFrame(_currentIdx);
      _preloadAll();
    } catch (e) {
      console.error('Player.loadProduct:', e);
      showToast('Errore caricamento prodotto: ' + e.message, 'error');
    }
  }

  /** Pre-carica tutti i frame in background */
  async function _preloadAll() {
    let loaded = 0;
    const total = _timestamps.length;
    _onLoadProgress?.(0, total);

    for (const ts of _timestamps) {
      try {
        await RadarAPI.loadGeoTiff(_productType, ts);
        _preloaded.add(ts);
      } catch {}
      loaded++;
      _onLoadProgress?.(loaded, total);
    }
  }

  async function _showFrame(idx) {
    if (!_timestamps.length) return;
    _currentIdx = Math.max(0, Math.min(idx, _timestamps.length - 1));
    const ts = _timestamps[_currentIdx];

    if (elSlider) elSlider.value = _currentIdx;
    if (elCurrentTime) elCurrentTime.textContent = _formatTime(ts);

    const prod = CONFIG.PRODUCTS[_productType];
    await _onFrameChange?.(ts, _currentIdx, _timestamps.length);
  }

  function _startPlay() {
    _isPlaying = true;
    if (elPlay) { elPlay.innerHTML = '<i class="fa fa-pause"></i>'; elPlay.title = 'Pausa'; }
    _playInterval = setInterval(async () => {
      const next = (_currentIdx + 1) % _timestamps.length;
      await _showFrame(next);
    }, _speedMs);
  }

  function _stopPlay() {
    _isPlaying = false;
    if (elPlay) { elPlay.innerHTML = '<i class="fa fa-play"></i>'; elPlay.title = 'Play'; }
    clearInterval(_playInterval);
    _playInterval = null;
  }

  function togglePlay() {
    if (_isPlaying) _stopPlay(); else _startPlay();
  }

  function stepBack() {
    _stopPlay();
    _showFrame(_currentIdx - 1);
  }

  function stepForward() {
    _stopPlay();
    _showFrame(_currentIdx + 1);
  }

  function goToLast() {
    _stopPlay();
    _showFrame(_timestamps.length - 1);
  }

  function goToFirst() {
    _stopPlay();
    _showFrame(0);
  }

  function getCurrentTimestamp() {
    return _timestamps[_currentIdx] ?? null;
  }

  function getTimestamps() { return [..._timestamps]; }

  function _formatTime(ms) {
    if (!ms) return '--';
    const d = new Date(ms);
    return d.toLocaleString('it-IT', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    }) + ' UTC';
  }

  return {
    init,
    loadProduct,
    togglePlay,
    stepBack,
    stepForward,
    goToLast,
    goToFirst,
    getCurrentTimestamp,
    getTimestamps,
  };
})();
