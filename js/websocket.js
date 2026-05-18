/**
 * websocket.js — Client WebSocket/STOMP per notifiche push Radar DPC
 *
 * L'endpoint wss://radar-wss.protezionecivile.it non è documentato e spesso
 * non è raggiungibile da origin esterni. Per questo:
 *   - si tenta UNA sola connessione, senza riconnessioni infinite
 *   - in caso di fallimento si passa silenziosamente al polling REST
 *   - lo status indicator mostra "polling" invece di "errore"
 */

const RadarWebSocket = (() => {

  const WSS_URL = CONFIG.WSS_URL || null;     // se null → WSS disabilitato
  const TOPIC = '/topic/product';
  const ATTEMPT_TIMEOUT_MS = 6000;

  let _client = null;
  let _handlers = new Map();
  let _globalHandlers = [];
  let _status = 'disabled';
  let _onStatusChange = null;
  let _attempted = false;

  function connect(onStatusChange) {
    _onStatusChange = onStatusChange;

    if (!WSS_URL) {
      _setStatus('polling');
      return;
    }
    if (_attempted && _status !== 'connected') {
      _setStatus('polling');
      return;
    }
    if (typeof StompJs === 'undefined') {
      _setStatus('polling');
      return;
    }

    _attempted = true;
    _setStatus('connecting');

    try {
      _client = new StompJs.Client({
        brokerURL: WSS_URL,
        reconnectDelay: 0,
        heartbeatIncoming: 10000,
        heartbeatOutgoing: 10000,

        onConnect: () => {
          _setStatus('connected');
          _client.subscribe(TOPIC, (frame) => {
            try {
              const msg = JSON.parse(frame.body);
              _dispatch(msg);
            } catch (_) {}
          });
        },

        onDisconnect: () => { _setStatus('polling'); },
        onStompError: () => { _silentFallback(); },
        onWebSocketError: () => { _silentFallback(); },
        onWebSocketClose: () => {
          if (_status !== 'connected') _silentFallback();
        },
      });

      _client.activate();

      setTimeout(() => {
        if (_status !== 'connected') _silentFallback();
      }, ATTEMPT_TIMEOUT_MS);

    } catch (_) {
      _silentFallback();
    }
  }

  function _silentFallback() {
    try { _client?.deactivate(); } catch (_) {}
    _client = null;
    _setStatus('polling');
  }

  function disconnect() {
    try { _client?.deactivate(); } catch (_) {}
    _client = null;
    _setStatus('disabled');
  }

  function on(productType, callback) {
    if (productType === null || productType === undefined || productType === '*') {
      _globalHandlers.push(callback);
    } else {
      if (!_handlers.has(productType)) _handlers.set(productType, []);
      _handlers.get(productType).push(callback);
    }
  }

  function off(productType, callback) {
    if (productType === null || productType === '*') {
      _globalHandlers = _globalHandlers.filter(c => c !== callback);
    } else if (_handlers.has(productType)) {
      _handlers.set(productType, _handlers.get(productType).filter(c => c !== callback));
    }
  }

  function _dispatch(msg) {
    const { productType } = msg;
    (_handlers.get(productType) || []).forEach(cb => { try { cb(msg); } catch (_) {} });
    _globalHandlers.forEach(cb => { try { cb(msg); } catch (_) {} });
  }

  function _setStatus(s) {
    if (s === _status) return;
    _status = s;
    if (typeof _onStatusChange === 'function') {
      try { _onStatusChange(s); } catch (_) {}
    }
  }

  function getStatus() { return _status; }

  /** Compatibilità con main.js che chiama RadarWebSocket.isConnected() */
  function isConnected() { return _status === 'connected'; }

  return { connect, disconnect, on, off, getStatus, isConnected };
})();
