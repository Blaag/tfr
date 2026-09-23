const MAX_EVENTS_PER_WORLD = 2500;
const MAX_RENDERED_EVENTS = 500;
const MAX_COMMAND_HISTORY = 50;
const MAX_STORED_COMMAND_CHARACTERS = 4096;
const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000, 15000];

const elements = {
  pairing: document.querySelector("#pairing"),
  pairingMessage: document.querySelector("#pairing-message"),
  console: document.querySelector("#console"),
  worldButton: document.querySelector("#world-button"),
  worldName: document.querySelector("#world-name"),
  worldSignal: document.querySelector("#world-signal"),
  connectionState: document.querySelector("#connection-state"),
  historyNotice: document.querySelector("#history-notice"),
  transcript: document.querySelector("#transcript"),
  eventList: document.querySelector("#event-list"),
  emptyState: document.querySelector("#empty-state"),
  returnLive: document.querySelector("#return-live"),
  returnLiveCount: document.querySelector("#return-live-count"),
  composer: document.querySelector("#composer"),
  commandInput: document.querySelector("#command-input"),
  sendButton: document.querySelector("#send-button"),
  historyOlder: document.querySelector("#history-older"),
  historyNewer: document.querySelector("#history-newer"),
  worldDialog: document.querySelector("#world-dialog"),
  worldList: document.querySelector("#world-list"),
  unpairDevice: document.querySelector("#unpair-device"),
  toast: document.querySelector("#toast"),
};

const state = {
  socket: null,
  reconnectTimer: null,
  reconnectAttempt: 0,
  intentionalClose: false,
  ready: false,
  gatewayId: readStorage("tfr.gatewayId"),
  cursor: null,
  worlds: [],
  events: new Map(),
  eventIds: new Map(),
  unread: new Map(),
  selectedWorld: readStorage("tfr.selectedWorld"),
  atLive: true,
  unseenLive: 0,
  drafts: {},
  commandHistory: {},
  historyIndex: null,
  pending: new Map(),
  pairingCode: null,
  toastTimer: null,
};

function readStorage(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeStore(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private browsing and managed devices may deny persistent storage.
  }
}

function safeRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Storage is optional; the live session remains usable without it.
  }
}

function setConnection(label, kind = "") {
  elements.connectionState.textContent = label;
  elements.connectionState.className = `connection-state ${kind}`.trim();
}

function showToast(message, duration = 2600) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, duration);
}

function currentWorld() {
  return state.worlds.find((world) => world.world === state.selectedWorld) || null;
}

function updateWorldHeader() {
  const world = currentWorld();
  elements.worldName.textContent = world?.world || "Select world";
  elements.worldSignal.className = `world-signal ${world?.state || ""}`.trim();
  elements.commandInput.disabled = !world || !state.ready;
  elements.sendButton.disabled = !world || !state.ready;
}

function renderWorlds() {
  elements.worldList.replaceChildren();
  for (const world of state.worlds) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `world-option${world.world === state.selectedWorld ? " active" : ""}`;

    const signal = document.createElement("span");
    signal.className = `world-list-signal ${world.state || ""}`;
    signal.setAttribute("aria-hidden", "true");

    const label = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = world.world;
    const status = document.createElement("small");
    status.textContent = world.state || "unknown";
    label.append(name, status);

    button.append(signal, label);
    const unread = state.unread.get(world.world) || 0;
    if (unread > 0) {
      const badge = document.createElement("span");
      badge.className = "unread-badge";
      badge.textContent = unread > 99 ? "99+" : String(unread);
      button.append(badge);
    }
    button.addEventListener("click", () => selectWorld(world.world));
    elements.worldList.append(button);
  }
}

function selectWorld(worldName) {
  if (state.selectedWorld) {
    state.drafts[state.selectedWorld] = elements.commandInput.value.slice(
      0,
      MAX_STORED_COMMAND_CHARACTERS,
    );
  }
  state.selectedWorld = worldName;
  safeStore("tfr.selectedWorld", worldName);
  state.unread.set(worldName, 0);
  state.unseenLive = 0;
  state.atLive = true;
  state.historyIndex = null;
  elements.commandInput.value = state.drafts[worldName] || "";
  updateWorldHeader();
  renderWorlds();
  renderTranscript({ scrollToLive: true });
  elements.worldDialog.close();
  elements.commandInput.focus({ preventScroll: true });
}

function appendTextWithLinks(container, text) {
  const pattern = /https?:\/\/[^\s<>"']+/gi;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) {
      container.append(document.createTextNode(text.slice(offset, match.index)));
    }
    try {
      const url = new URL(match[0]);
      if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error();
      const link = document.createElement("a");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = match[0];
      container.append(link);
    } catch {
      container.append(document.createTextNode(match[0]));
    }
    offset = match.index + match[0].length;
  }
  if (offset < text.length) container.append(document.createTextNode(text.slice(offset)));
}

function eventNode(event) {
  const item = document.createElement("li");
  item.className = `event ${event.direction}`;
  item.dataset.eventId = event.id;

  const time = document.createElement("time");
  const timestamp = new Date(event.timestamp);
  time.dateTime = event.timestamp;
  time.textContent = Number.isNaN(timestamp.valueOf())
    ? "--:--"
    : timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const body = document.createElement("div");
  body.className = "event-body";
  const text = document.createElement("p");
  text.className = "event-text";
  appendTextWithLinks(text, event.text || "");
  body.append(text);

  const sender = event.provenance?.sender_name;
  const source = event.provenance?.server_source;
  if (sender || source || event.redacted) {
    const meta = document.createElement("div");
    meta.className = "event-meta";
    meta.textContent = [sender, source, event.redacted ? "redacted" : null]
      .filter(Boolean)
      .join(" · ");
    body.append(meta);
  }
  item.append(time, body);
  return item;
}

function renderTranscript({ scrollToLive = false } = {}) {
  const events = state.events.get(state.selectedWorld) || [];
  const visible = events.slice(-MAX_RENDERED_EVENTS);
  elements.eventList.replaceChildren(...visible.map(eventNode));
  elements.emptyState.hidden = visible.length > 0;
  if (scrollToLive) {
    requestAnimationFrame(() => {
      elements.transcript.scrollTop = elements.transcript.scrollHeight;
      state.atLive = true;
      state.unseenLive = 0;
      updateReturnLive();
    });
  }
}

function appendLiveEvent(event) {
  elements.eventList.append(eventNode(event));
  while (elements.eventList.children.length > MAX_RENDERED_EVENTS) {
    elements.eventList.firstElementChild?.remove();
  }
  elements.emptyState.hidden = true;
  requestAnimationFrame(() => {
    elements.transcript.scrollTop = elements.transcript.scrollHeight;
  });
}

function updateReturnLive() {
  elements.returnLive.hidden = state.atLive || state.unseenLive === 0;
  elements.returnLiveCount.textContent = state.unseenLive > 0 ? String(state.unseenLive) : "";
}

function addEvent(message) {
  const event = message.event;
  if (!event || typeof event.world !== "string") return;
  let events = state.events.get(event.world);
  if (!events) {
    events = [];
    state.events.set(event.world, events);
    state.eventIds.set(event.world, new Set());
  }
  const eventIds = state.eventIds.get(event.world);
  if (eventIds.has(event.id)) return;
  events.push(event);
  eventIds.add(event.id);
  if (events.length > MAX_EVENTS_PER_WORLD) {
    for (const removed of events.splice(0, events.length - MAX_EVENTS_PER_WORLD)) {
      eventIds.delete(removed.id);
    }
  }
  if (event.connection_state) {
    const world = state.worlds.find((item) => item.world === event.world);
    if (world) world.state = event.connection_state;
  }

  if (state.ready) {
    state.cursor = message.cursor;
  } else {
    return;
  }
  if (event.world !== state.selectedWorld) {
    state.unread.set(event.world, (state.unread.get(event.world) || 0) + 1);
    renderWorlds();
    return;
  }
  if (state.ready && !state.atLive) {
    state.unseenLive += 1;
    updateReturnLive();
  }
  if (state.atLive) appendLiveEvent(event);
  updateWorldHeader();
}

function resetGatewayState() {
  state.events.clear();
  state.eventIds.clear();
  state.unread.clear();
  state.cursor = null;
  safeRemove("tfr.cursor");
}

function handleMessage(message) {
  if (!message || message.protocol !== 1 || typeof message.type !== "string") return;
  if (message.type === "hello") {
    state.ready = false;
    if (message.history_reset || (state.gatewayId && state.gatewayId !== message.gateway_id)) {
      resetGatewayState();
    }
    state.gatewayId = message.gateway_id;
    safeStore("tfr.gatewayId", state.gatewayId);
    state.worlds = Array.isArray(message.worlds) ? message.worlds : [];
    if (!state.worlds.some((world) => world.world === state.selectedWorld)) {
      state.selectedWorld = state.worlds[0]?.world || null;
    }
    if (state.selectedWorld) {
      safeStore("tfr.selectedWorld", state.selectedWorld);
      elements.commandInput.value = state.drafts[state.selectedWorld] || "";
    }
    elements.historyNotice.hidden = !message.history_truncated && !message.history_reset;
    elements.historyNotice.textContent = message.history_reset
      ? "The Gateway restarted. Showing a fresh retained history."
      : "Older retained history was omitted from this mobile snapshot.";
    renderWorlds();
    updateWorldHeader();
    return;
  }
  if (message.type === "event") {
    addEvent(message);
    return;
  }
  if (message.type === "cursor") {
    state.cursor = message.cursor;
    return;
  }
  if (message.type === "ready") {
    state.ready = true;
    state.cursor = message.cursor;
    state.reconnectAttempt = 0;
    elements.pairing.hidden = true;
    elements.console.hidden = false;
    setConnection("Live", "online");
    updateWorldHeader();
    renderTranscript({ scrollToLive: true });
    return;
  }
  if (message.type === "ack") {
    const pending = state.pending.get(message.request_id);
    state.pending.delete(message.request_id);
    if (message.ok !== true) {
      showToast(message.error || "Command was rejected", 4200);
      if (pending) {
        state.drafts[pending.world] = pending.text.slice(0, MAX_STORED_COMMAND_CHARACTERS);
        if (pending.world === state.selectedWorld) elements.commandInput.value = pending.text;
      }
    } else if (pending) {
      showToast("Command accepted");
    }
    return;
  }
  if (message.type === "error") {
    showToast(message.message || "Gateway protocol error", 4200);
  }
}

function socketUrl() {
  const url = new URL("/ws", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (state.gatewayId) url.searchParams.set("gateway_id", state.gatewayId);
  if (state.gatewayId && state.cursor) url.searchParams.set("after_cursor", state.cursor);
  return url;
}

function connect() {
  if (state.socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.socket.readyState)) return;
  window.clearTimeout(state.reconnectTimer);
  state.intentionalClose = false;
  state.ready = false;
  setConnection("Connecting");
  updateWorldHeader();
  const socket = new WebSocket(socketUrl());
  state.socket = socket;

  socket.addEventListener("message", (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch {
      showToast("Received an invalid Gateway message", 4200);
    }
  });
  socket.addEventListener("close", async (event) => {
    if (state.socket !== socket) return;
    state.socket = null;
    state.ready = false;
    updateWorldHeader();
    if (state.pending.size > 0) {
      for (const pending of state.pending.values()) {
        state.drafts[pending.world] = pending.text.slice(0, MAX_STORED_COMMAND_CHARACTERS);
        if (pending.world === state.selectedWorld) elements.commandInput.value = pending.text;
      }
      state.pending.clear();
      showToast("Command status unknown. Check the transcript before resending.", 6000);
    }
    const session =
      event.code === 1008 || state.reconnectAttempt >= 2 ? await sessionState() : "paired";
    if (state.socket && state.socket !== socket) return;
    if (session === "unpaired") {
      await clearLocalData();
      elements.console.hidden = true;
      elements.pairing.hidden = false;
      elements.pairingMessage.textContent =
        "This device session expired or was revoked. Create a new pairing link on the Gateway.";
      return;
    }
    setConnection(navigator.onLine ? "Reconnecting" : "Offline", "error");
    if (!state.intentionalClose) scheduleReconnect();
  });
  socket.addEventListener("error", () => socket.close());
}

function scheduleReconnect() {
  window.clearTimeout(state.reconnectTimer);
  const delay = RECONNECT_DELAYS[Math.min(state.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
  state.reconnectAttempt += 1;
  state.reconnectTimer = window.setTimeout(connect, delay);
}

function sendCommand(event) {
  event.preventDefault();
  const world = currentWorld();
  const text = elements.commandInput.value;
  if (!world || !state.ready || !state.socket || state.socket.readyState !== WebSocket.OPEN) {
    showToast("Gateway is not ready");
    return;
  }
  if (!text || /[\r\n\0]/.test(text)) {
    showToast("Commands must be a single non-empty line");
    return;
  }
  if ([...state.pending.values()].some((pending) => pending.world === world.world)) {
    showToast("Wait for the previous command acknowledgement");
    return;
  }
  const requestId = crypto.randomUUID();
  state.pending.set(requestId, { world: world.world, text });
  try {
    state.socket.send(
      JSON.stringify({ type: "command", request_id: requestId, world: world.world, text }),
    );
  } catch {
    state.pending.delete(requestId);
    showToast("Command was not sent");
    return;
  }

  const history = Array.isArray(state.commandHistory[world.world])
    ? state.commandHistory[world.world]
    : [];
  const storedText = text.slice(0, MAX_STORED_COMMAND_CHARACTERS);
  if (history.at(-1) !== storedText) history.push(storedText);
  state.commandHistory[world.world] = history.slice(-MAX_COMMAND_HISTORY);
  state.drafts[world.world] = "";
  state.historyIndex = null;
  elements.commandInput.value = "";
}

function moveHistory(direction) {
  const world = currentWorld();
  if (!world) return;
  const history = state.commandHistory[world.world] || [];
  if (!history.length) return;
  if (state.historyIndex === null) {
    state.historyIndex = direction < 0 ? history.length - 1 : history.length;
  } else {
    state.historyIndex = Math.max(0, Math.min(history.length, state.historyIndex + direction));
  }
  elements.commandInput.value = state.historyIndex === history.length ? "" : history[state.historyIndex];
  elements.commandInput.focus({ preventScroll: true });
}

async function pairFromFragment(code) {
  elements.pairing.hidden = false;
  elements.pairingMessage.textContent = "Pairing this device with the Gateway…";
  let response;
  try {
    response = await fetch("/api/pair", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    elements.pairingMessage.textContent = "Gateway unavailable. Pairing will retry when online.";
    return "unreachable";
  }
  try {
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "Pairing failed");
    return "paired";
  } catch (error) {
    elements.pairingMessage.textContent = error instanceof Error ? error.message : "Pairing failed";
    return "rejected";
  }
}

async function sessionState() {
  try {
    const response = await fetch("/api/session", { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) return "unreachable";
    return (await response.json()).paired === true ? "paired" : "unpaired";
  } catch {
    return "unreachable";
  }
}

async function clearLocalData() {
  state.socket?.close();
  state.gatewayId = null;
  state.cursor = null;
  state.selectedWorld = null;
  state.drafts = {};
  state.commandHistory = {};
  state.pending.clear();
  for (const key of [
    "tfr.gatewayId",
    "tfr.cursor",
    "tfr.selectedWorld",
    "tfr.drafts",
    "tfr.commandHistory",
  ]) {
    safeRemove(key);
  }
  if ("caches" in window) {
    for (const key of await caches.keys()) await caches.delete(key);
  }
}

async function unpairDevice() {
  if (!window.confirm("Unpair this phone and clear its local TFR data?")) return;
  state.intentionalClose = true;
  try {
    const response = await fetch("/api/logout", {
      method: "POST",
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("Logout was rejected");
    await clearLocalData();
    location.reload();
  } catch {
    state.intentionalClose = false;
    showToast("Could not revoke this device. Reconnect or revoke it from the Gateway.", 6000);
  }
}

async function start() {
  for (const key of ["tfr.cursor", "tfr.drafts", "tfr.commandHistory"]) safeRemove(key);
  const pairingCode = new URLSearchParams(location.hash.slice(1)).get("pair");
  if (pairingCode) history.replaceState(null, "", `${location.pathname}${location.search}`);
  state.pairingCode = pairingCode;
  let session = await sessionState();
  if (pairingCode) {
    const pairing = await pairFromFragment(pairingCode);
    if (pairing !== "unreachable") state.pairingCode = null;
    if (pairing === "paired") session = "paired";
  }
  const paired = session === "paired";
  if (session === "unreachable") {
    elements.pairing.hidden = !state.pairingCode;
    elements.console.hidden = Boolean(state.pairingCode);
    if (!state.pairingCode) {
      setConnection("Offline", "error");
      scheduleReconnect();
    }
    return;
  }
  elements.pairing.hidden = paired;
  elements.console.hidden = !paired;
  if (!paired) {
    await clearLocalData();
    if (!pairingCode) {
      elements.pairingMessage.textContent =
        'On the Gateway host, run tfr pair --device-name "My iPhone", then open that link here.';
    }
    return;
  }
  connect();
}

async function resume() {
  if (state.pairingCode) {
    const pairing = await pairFromFragment(state.pairingCode);
    if (pairing === "unreachable") return;
    state.pairingCode = null;
    if (pairing !== "paired") return;
    elements.pairing.hidden = true;
    elements.console.hidden = false;
  }
  connect();
}

elements.worldButton.addEventListener("click", () => elements.worldDialog.showModal());
elements.unpairDevice.addEventListener("click", unpairDevice);
elements.composer.addEventListener("submit", sendCommand);
elements.commandInput.addEventListener("input", () => {
  if (!state.selectedWorld) return;
  state.drafts[state.selectedWorld] = elements.commandInput.value.slice(
    0,
    MAX_STORED_COMMAND_CHARACTERS,
  );
  state.historyIndex = null;
});
elements.commandInput.addEventListener("keydown", (event) => {
  if (event.key === "ArrowUp" && elements.commandInput.selectionStart === 0) {
    event.preventDefault();
    moveHistory(-1);
  } else if (
    event.key === "ArrowDown" &&
    elements.commandInput.selectionStart === elements.commandInput.value.length
  ) {
    event.preventDefault();
    moveHistory(1);
  }
});
elements.historyOlder.addEventListener("click", () => moveHistory(-1));
elements.historyNewer.addEventListener("click", () => moveHistory(1));
elements.returnLive.addEventListener("click", () => renderTranscript({ scrollToLive: true }));
elements.transcript.addEventListener("scroll", () => {
  const distance = elements.transcript.scrollHeight - elements.transcript.scrollTop - elements.transcript.clientHeight;
  state.atLive = distance < 36;
  if (state.atLive) state.unseenLive = 0;
  updateReturnLive();
});
window.addEventListener("online", resume);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") resume();
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js"));
}

start();
