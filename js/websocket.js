/**
 * websocket.js — Client WebSocket/STOMP per notifiche push Radar DPC
 * wss://radar-wss.protezionecivile.it
 *
 * Sostituisce il polling REST con un canale push: la piattaforma riceve
 * esattamente il timestamp corretto del nuovo campione, senza errori 404
 * per timestamp non ancora disponibili.
 */

const RadarWebSocket = (() => {

  const WSS_URL = 'wss://radar-wss.protezionecivile.it';
  const TOPIC   = '/topic/product';
  const RECONNECT_DELAY_MS = 8000;

  let _client = null;
  let _handlers = new Map(); // productType → [callback, ...]
  let _globalHandlers = [];  // callback per qualsiasi prodotto
  let _status = 'disconnected'; // 'disconnected' | 'connecting' | 'connected'
  let _onStatusChange = null;

  /** Avvia la connessione WebSocket/STOMP */
  function connect(onStatusChange) {
    if (_client && _client.active) return;
    _onStatusChange = onStatusChange;

    if (typeof StompJs === 'undefined') {
      console.warn('RadarWebSocket: StompJs non disponibile, fallback a polling REST');
      _setStatus('error');
      return;
    }

    _setStatus('connecting');

    _client = new StompJs.Client({
      brokerURL: WSS_URL,
      reconnectDelay: RECONNECT_DELAY_MS,
      heartbeatIncoming: 10000,
      heartbeatOutgoing: 10000,

      onConnect: () => {
        _setStatus('connected');
        console.log('[WSS] Connesso a', WSS_URL);

        _client.subscribe(TOPIC, (frame) => {
          try {
            const msg = JSON.parse(frame.body);
            // { productType: "VMI", time: 1758706200000, period: "PT5M" }
            _dispatch(msg);
          } catch (e) {
            console.error('[WSS] Parse error:', e);
          }
        });
      },

      onDisconnect: () => {
        _setStatus('disconnected');
        console.log('[WSS] Disconnesso');
      },

      onStompError: (frame) => {
        console.error('[WSS] STOMP error:', frame.headers?.message);
        _setStatus('error');
      },

      onWebSocketError: (evt) => {
        console.warn('[WSS] WebSocket error — riconnessione in', RECONNECT_DELAY_MS / 1000, 's');
        _setStatus('reconnecting');
      },
    });

    _client.activate();
  }

  /** Disconnette e pulisce */
  function disconnect() {
    if (_client) {
      _client.deactivate();
      _client = null;
    }
    _setStatus('disconnected');
  }

  /**
   * Registra un handler per aggiornamenti di uno specifico prodotto.
   * @param {string|null} productType  - es. "VMI", null = tutti i prodotti
   * @param {Function} callback        - fn({ productType, time, period })
   * @returns {Function} unsubscribe
   */
  function on(productType, callback) {
    if (productType === null) {
      _globalHandlers.push(callback);
      return () => { _globalHandlers = _globalHandlers.filter(h => h !== callback); };
    }
    if (!_handlers.has(productType)) _handlers.set(productType, []);
    _handlers.get(productType).push(callback);
    return () => {
      const arr = _handlers.get(productType);
      if (arr) _handlers.set(productType, arr.filter(h => h !== callback));
    };
  }

  function _dispatch(msg) {
    const { productType } = msg;
    // Handler specifici
    (_handlers.get(productType) ?? []).forEach(h => { try { h(msg); } catch(e) { console.error(e); } });
    // Handler globali
    _globalHandlers.forEach(h => { try { h(msg); } catch(e) { console.error(e); } });
  }

  function _setStatus(s) {
    _status = s;
    _onStatusChange?.(s);
    _updateStatusUI(s);
  }

  function _updateStatusUI(s) {
    const el = document.getElementById('wss-status');
    if (!el) return;
    const labels = {
      connecting:   { icon: '🔄', text: 'WSS connessione…', cls: 'warn' },
      connected:    { icon: '🟢', text: 'WSS live',         cls: 'ok'   },
      disconnected: { icon: '⚫', text: 'WSS offline',      cls: 'err'  },
      reconnecting: { icon: '🔁', text: 'WSS riconnessione…', cls: 'warn' },
      error:        { icon: '🔴', text: 'WSS errore',       cls: 'err'  },
    };
    const { icon, text, cls } = labels[s] ?? labels.error;
    el.innerHTML = `${icon} ${text}`;
    el.className = `wss-indicator wss-${cls}`;
  }

  function getStatus() { return _status; }
  function isConnected() { return _status === 'connected'; }

  return { connect, disconnect, on, getStatus, isConnected };
})();
