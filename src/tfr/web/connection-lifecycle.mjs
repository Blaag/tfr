export function createConnectionLifecycle({
  connectionTimeout,
  reconnectDelays,
  setTimer,
  clearTimer,
  onConnectionTimeout,
  onReconnect,
}) {
  let attemptGeneration = 0;
  let probeGeneration = 0;
  let connectionTimer = null;
  let reconnectTimer = null;

  const lifecycle = {
    socket: null,
    ready: false,
    reconnectAttempt: 0,
    intentionalClose: false,

    begin(socket) {
      lifecycle.cancelReconnect();
      lifecycle.invalidateProbes();
      lifecycle.intentionalClose = false;
      lifecycle.socket = socket;
      lifecycle.ready = false;
      const generation = ++attemptGeneration;
      clearTimer(connectionTimer);
      connectionTimer = setTimer(() => {
        connectionTimer = null;
        if (generation !== attemptGeneration || lifecycle.socket !== socket || lifecycle.ready) {
          return;
        }
        lifecycle.socket = null;
        onConnectionTimeout(socket);
        if (!lifecycle.intentionalClose) lifecycle.scheduleReconnect();
      }, connectionTimeout);
      return generation;
    },

    isCurrent(socket) {
      return lifecycle.socket === socket;
    },

    markReady(socket) {
      if (!lifecycle.isCurrent(socket)) return false;
      clearTimer(connectionTimer);
      connectionTimer = null;
      lifecycle.ready = true;
      lifecycle.reconnectAttempt = 0;
      return true;
    },

    close(socket) {
      if (!lifecycle.isCurrent(socket)) return null;
      clearTimer(connectionTimer);
      connectionTimer = null;
      lifecycle.socket = null;
      lifecycle.ready = false;
      const generation = ++attemptGeneration;
      return { generation, probe: lifecycle.beginProbe() };
    },

    closeIsCurrent(token) {
      return (
        token !== null &&
        token.generation === attemptGeneration &&
        token.probe === probeGeneration &&
        lifecycle.socket === null
      );
    },

    beginProbe() {
      probeGeneration += 1;
      return probeGeneration;
    },

    probeIsCurrent(generation) {
      return generation === probeGeneration;
    },

    invalidateProbes() {
      probeGeneration += 1;
    },

    scheduleReconnect() {
      if (lifecycle.intentionalClose) return;
      clearTimer(reconnectTimer);
      const delay = reconnectDelays[
        Math.min(lifecycle.reconnectAttempt, reconnectDelays.length - 1)
      ];
      lifecycle.reconnectAttempt += 1;
      reconnectTimer = setTimer(() => {
        reconnectTimer = null;
        onReconnect();
      }, delay);
    },

    cancelReconnect() {
      clearTimer(reconnectTimer);
      reconnectTimer = null;
    },

    hasReconnectScheduled() {
      return reconnectTimer !== null;
    },

    stop() {
      lifecycle.intentionalClose = true;
      lifecycle.cancelReconnect();
      clearTimer(connectionTimer);
      connectionTimer = null;
      lifecycle.invalidateProbes();
      attemptGeneration += 1;
      const socket = lifecycle.socket;
      lifecycle.socket = null;
      lifecycle.ready = false;
      return socket;
    },
  };

  return lifecycle;
}
