/**
 * websocket.js — Client WebSocket/STOMP per notifiche push Radar DPC
 *
 * NOTA: l'endpoint wss://radar-wss.protezionecivile.it non è ufficialmente
 * documentato e in molti casi non è raggiungibile da origin esterni.
 * Per questo motivo:
 *   - si tenta UNA sola connessione, senza riconnessioni infinite
 *   - in caso di fallimento si passa silenziosamente al polling REST
 *   - lo status indicator mostra "polling" invece di "errore"
 *
 * Se DPC pubblica in futuro un endpoint ufficiale lo si imposta in
 * CONFIG.WSS_URL e si riabilita auto-reconnect a true.
 */

const RadarWebSocket = (() => {

  const WSS_URL = CONFIG.WSS_URL || null;     // se null → WSS disabilitato
  const TOPIC = '/topic/product';
  const ATTEMPT_TIMEOUT_MS = 6000;            // se non si connette in 6 s, addio

  let _client = null;
  let _handlers = new Map();
  let _globalHandlers = [];
  let _status = 'disabled';
  let _onStatusChange = null;
  let _attempted = false;

  function connect(onStatusChange) {
    _onStatusChange = onStatusChange;

    // Caso 1: WSS disabilitato esplicitamente → resto in stato 'polling'
    if (!WSS_URL) {
      _setStatus('polling');
      return;
    }

    // Caso 2: già tentato una volta e fallito → non riprovo
    if (_attempted && _status !== 'connected') {
      _setStatus('polling');
      return;
    }

    // Caso 3: libreria STOMP non caricata → polling
    if (typeof StompJs === 'undefined') {
      _setStatus('polling');
      return;
    }

    _attempted = true;
    _setStatus('connecting');

    try {
      _client = new StompJs.Client({
        brokerURL: WSS_URL,
        reconnectDelay: 0,           // niente auto-reconnect
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

      // safety net: se entro N secondi non si è connesso, abortisco
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

  return { connect, disconnect, on, off, getStatus };
})();
