import { createPairingSubmission, pairingCodeFromLink, submitPairing } from "./pairing.mjs";
import { normalizeWorldCommand } from "./command.mjs";
import { eventDetailRows } from "./event-details.mjs";
import {
  generationIsNewer,
  historyNoticeDecision,
  historySnapshotHasGap,
  recordLiveHistoryEvent,
} from "./history-notice.mjs";
import { linkParts } from "./linkify.mjs";
import { swipeDirection } from "./swipe.mjs";

const MAX_EVENTS_PER_WORLD = 2500;
const MAX_RENDERED_EVENTS = 500;
const MAX_COMMAND_HISTORY = 50;
const MAX_STORED_COMMAND_CHARACTERS = 4096;
const MAX_PAIRING_ATTEMPTS = 3;
const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000, 15000];
const HEARTBEAT_INTERVAL_MS = 20000;
const HEARTBEAT_TIMEOUT_MS = 8000;
const MIN_TEXT_SIZE = -2;
const MAX_TEXT_SIZE = 4;

const elements = {
  pairing: document.querySelector("#pairing"),
  pairingMessage: document.querySelector("#pairing-message"),
  pairingForm: document.querySelector("#pairing-form"),
  pairingLink: document.querySelector("#pairing-link"),
  console: document.querySelector("#console"),
  settingsButton: document.querySelector("#settings-button"),
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
  historyButton: document.querySelector("#history-button"),
  historyDialog: document.querySelector("#history-dialog"),
  historyDialogWorld: document.querySelector("#history-dialog-world"),
  historyEmpty: document.querySelector("#history-empty"),
  historyList: document.querySelector("#history-list"),
  settingsDialog: document.querySelector("#settings-dialog"),
  worldList: document.querySelector("#world-list"),
  gatewayVersion: document.querySelector("#gateway-version"),
  eventDialog: document.querySelector("#event-dialog"),
  eventDialogWorld: document.querySelector("#event-dialog-world"),
  eventDetails: document.querySelector("#event-details"),
  textSmaller: document.querySelector("#text-smaller"),
  textReset: document.querySelector("#text-reset"),
  textLarger: document.querySelector("#text-larger"),
  motionPreference: document.querySelector("#motion-preference"),
  lineWrap: document.querySelector("#line-wrap"),
  unpairDevice: document.querySelector("#unpair-device"),
  updateNotice: document.querySelector("#update-notice"),
  applyUpdate: document.querySelector("#apply-update"),
  toast: document.querySelector("#toast"),
};

const state = {
  socket: null,
  reconnectTimer: null,
  reconnectAttempt: 0,
  intentionalClose: false,
  ready: false,
  historyReset: false,
  gatewayId: readStorage("tfr.gatewayId"),
  cursor: null,
  worlds: [],
  events: new Map(),
  eventIds: new Map(),
  unread: new Map(),
  reading: new Map(),
  selectedWorld: readStorage("tfr.selectedWorld"),
  drafts: {},
  commandHistory: {},
  historyIndex: null,
  pending: new Map(),
  toastTimer: null,
  heartbeatTimer: null,
  heartbeatTimeout: null,
  heartbeatRequestId: null,
  serviceWorkerRegistration: null,
  reloadingForUpdate: false,
  shownHistoryNotices: new Map(),
};
const pairingSubmission = createPairingSubmission(
  (code) => submitPairing(document, code),
  (callback, delay) => window.setTimeout(callback, delay),
);
let pairingRecoveryTimer = null;
let viewportTimer = null;
let historyNoticeTimer = null;
let historyNoticeKey = null;
let transcriptTouch = null;
let suppressTranscriptClickUntil = 0;
let transcriptPointerTarget = null;
let pendingLiveEvents = [];
let liveRenderFrame = null;

function updateViewportHeight() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight;
  const inputFocused = document.activeElement === elements.commandInput;
  const offsetTop = inputFocused ? viewport?.offsetTop || 0 : 0;
  document.documentElement.style.setProperty("--viewport-height", `${height}px`);
  document.documentElement.style.setProperty("--viewport-offset-top", `${offsetTop}px`);
}

function scheduleViewportUpdate() {
  updateViewportHeight();
  window.requestAnimationFrame(updateViewportHeight);
  window.clearTimeout(viewportTimer);
  viewportTimer = window.setTimeout(updateViewportHeight, 250);
}

function setInputModality(modality) {
  document.documentElement.dataset.inputModality = modality;
}

function showHistoryNotice(message, duration = 0, afterHide = null) {
  window.clearTimeout(historyNoticeTimer);
  historyNoticeTimer = null;
  elements.historyNotice.textContent = message;
  elements.historyNotice.hidden = !message;
  if (message && duration > 0) {
    historyNoticeTimer = window.setTimeout(() => {
      elements.historyNotice.hidden = true;
      elements.historyNotice.textContent = "";
      historyNoticeTimer = null;
      historyNoticeKey = null;
      afterHide?.();
    }, duration);
  }
}

function updateHistoryNotice() {
  if (state.historyReset) return;
  const world = currentWorld();
  const decision = historyNoticeDecision(
    world,
    state.events.get(state.selectedWorld) || [],
    state.shownHistoryNotices.get(world?.world),
    historyNoticeKey,
  );
  if (decision.action === "keep") return;
  historyNoticeKey = decision.key;
  if (decision.action === "hide") {
    showHistoryNotice("");
  } else {
    state.shownHistoryNotices.set(world.world, decision.generation);
    showHistoryNotice(decision.message, 10000);
  }
}

scheduleViewportUpdate();
window.addEventListener("resize", scheduleViewportUpdate);
window.addEventListener("orientationchange", scheduleViewportUpdate);
window.visualViewport?.addEventListener("resize", scheduleViewportUpdate);
window.visualViewport?.addEventListener("scroll", scheduleViewportUpdate);

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

function readingState(world = state.selectedWorld) {
  if (!world) return { atLive: true, unseenLive: 0, scrollTop: 0 };
  let reading = state.reading.get(world);
  if (!reading) {
    reading = { atLive: true, unseenLive: 0, scrollTop: 0 };
    state.reading.set(world, reading);
  }
  return reading;
}

function textSizePreference() {
  const parsed = Number.parseInt(readStorage("tfr.textSize") || "0", 10);
  return Number.isInteger(parsed) ? Math.max(MIN_TEXT_SIZE, Math.min(MAX_TEXT_SIZE, parsed)) : 0;
}

function applyTextSize(value) {
  const size = Math.max(MIN_TEXT_SIZE, Math.min(MAX_TEXT_SIZE, value));
  if (size === 0) {
    document.documentElement.removeAttribute("data-text-size");
    safeRemove("tfr.textSize");
  } else {
    document.documentElement.dataset.textSize = String(size);
    safeStore("tfr.textSize", String(size));
  }
  elements.textSmaller.disabled = size === MIN_TEXT_SIZE;
  elements.textLarger.disabled = size === MAX_TEXT_SIZE;
  elements.textReset.disabled = size === 0;
}

function applyMotionPreference(value) {
  const motion = ["system", "reduced", "full"].includes(value) ? value : "system";
  if (motion === "system") {
    document.documentElement.removeAttribute("data-motion");
    safeRemove("tfr.motion");
  } else {
    document.documentElement.dataset.motion = motion;
    safeStore("tfr.motion", motion);
  }
  elements.motionPreference.value = motion;
}

function lineWrapPreference() {
  return readStorage("tfr.lineWrap") !== "off";
}

function applyLineWrap(enabled) {
  elements.lineWrap.checked = enabled;
  if (enabled) {
    document.documentElement.removeAttribute("data-line-wrap");
    safeRemove("tfr.lineWrap");
  } else {
    document.documentElement.dataset.lineWrap = "off";
    safeStore("tfr.lineWrap", "off");
  }
}

applyTextSize(textSizePreference());
applyMotionPreference(readStorage("tfr.motion") || "system");
applyLineWrap(lineWrapPreference());

function readSessionStorage(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function storeSession(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // The initial navigation can still succeed when tab-scoped storage is unavailable.
  }
}

function clearPairingState() {
  window.clearTimeout(pairingRecoveryTimer);
  pairingRecoveryTimer = null;
  pairingSubmission.reset();
  try {
    sessionStorage.removeItem("tfr.pairingCode");
    sessionStorage.removeItem("tfr.pairingAttempts");
  } catch {
    // Nothing else retains the pairing secret after the fragment is removed.
  }
}

function postPairing(code, delay = 0) {
  const parsedAttempts = Number.parseInt(
    readSessionStorage("tfr.pairingAttempts") || "0",
    10,
  );
  const attempts = Number.isFinite(parsedAttempts) ? Math.max(0, parsedAttempts) : 0;
  if (attempts >= MAX_PAIRING_ATTEMPTS) {
    clearPairingState();
    elements.pairing.hidden = false;
    elements.console.hidden = true;
    elements.pairingMessage.textContent =
      "Pairing could not be completed. Create a new pairing link on the Gateway.";
    return;
  }
  if (!pairingSubmission.start(code, delay)) return;
  storeSession("tfr.pairingAttempts", String(attempts + 1));
  elements.pairing.hidden = false;
  elements.console.hidden = true;
  elements.pairingMessage.textContent =
    delay > 0 ? "Pairing is busy. Retrying shortly…" : "Pairing this device with the Gateway…";
  pairingRecoveryTimer = window.setTimeout(() => {
    pairingSubmission.reset();
    resume();
  }, delay + 15000);
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
  elements.historyButton.disabled = !world;
}

function restoreHistoryCommand(text) {
  if (!state.selectedWorld) return;
  elements.commandInput.value = text;
  state.drafts[state.selectedWorld] = text.slice(0, MAX_STORED_COMMAND_CHARACTERS);
  state.historyIndex = null;
  elements.historyDialog.close();
  window.requestAnimationFrame(() => {
    elements.commandInput.focus({ preventScroll: true });
    elements.commandInput.setSelectionRange(text.length, text.length);
    scheduleViewportUpdate();
  });
}

function openHistoryDialog() {
  const world = currentWorld();
  if (!world || elements.historyDialog.open) return;
  elements.commandInput.blur();
  elements.historyDialogWorld.textContent = world.world;
  const history = state.commandHistory[world.world] || [];
  const buttons = [...history].reverse().map((text) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.addEventListener("click", () => restoreHistoryCommand(text));
    return button;
  });
  elements.historyList.replaceChildren(...buttons);
  elements.historyEmpty.hidden = buttons.length > 0;
  elements.historyDialog.showModal();
  scheduleViewportUpdate();
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

function selectWorld(worldName, { focusInput = true } = {}) {
  if (state.selectedWorld) {
    state.drafts[state.selectedWorld] = elements.commandInput.value.slice(
      0,
      MAX_STORED_COMMAND_CHARACTERS,
    );
    readingState().scrollTop = elements.transcript.scrollTop;
  }
  state.selectedWorld = worldName;
  safeStore("tfr.selectedWorld", worldName);
  state.unread.set(worldName, 0);
  state.historyIndex = null;
  elements.commandInput.value = state.drafts[worldName] || "";
  updateWorldHeader();
  renderWorlds();
  renderTranscript({ restorePosition: true });
  updateHistoryNotice();
  if (elements.settingsDialog.open) elements.settingsDialog.close();
  if (focusInput) elements.commandInput.focus({ preventScroll: true });
}

function switchWorld(direction) {
  if (state.worlds.length < 2 || !state.selectedWorld) return;
  const current = state.worlds.findIndex((world) => world.world === state.selectedWorld);
  if (current < 0) return;
  const next = (current + direction + state.worlds.length) % state.worlds.length;
  elements.commandInput.blur();
  selectWorld(state.worlds[next].world, { focusInput: false });
  scheduleViewportUpdate();
}

function appendTextWithLinks(container, text) {
  for (const part of linkParts(text)) {
    if (part.type === "link") {
      const link = document.createElement("a");
      link.href = part.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = part.text;
      container.append(link);
    } else {
      container.append(document.createTextNode(part.text));
    }
  }
}

function appendEventText(container, event) {
  const runs = event.text_runs;
  if (
    !Array.isArray(runs) ||
    !runs.every(
      (run) =>
        run &&
        typeof run.text === "string" &&
        (run.role === undefined || run.role === "speaker"),
    ) ||
    runs.map((run) => run.text).join("") !== (event.text || "")
  ) {
    appendTextWithLinks(container, event.text || "");
    return;
  }
  for (const run of runs) {
    if (run.role === "speaker") {
      const speaker = document.createElement("span");
      if (event.spoof_status === "spoofed") speaker.className = "spoofed-speaker";
      appendTextWithLinks(speaker, run.text);
      container.append(speaker);
    } else {
      appendTextWithLinks(container, run.text);
    }
  }
}

function eventNode(event) {
  const item = document.createElement("li");
  item.className = `event ${event.direction}`;
  item.dataset.eventId = event.id;
  item.tabIndex = 0;
  item.setAttribute("role", "button");
  item.setAttribute("aria-haspopup", "dialog");
  item.setAttribute(
    "aria-label",
    `${event.spoof_status === "spoofed" ? "Spoofed speaker. " : ""}Inspect ${event.kind?.replaceAll("_", " ") || "event"} details`,
  );

  const body = document.createElement("div");
  body.className = "event-body";
  const text = document.createElement("p");
  text.className = "event-text";
  appendEventText(text, event);
  body.append(text);

  item.append(body);
  return item;
}

function eventById(eventId) {
  return (state.events.get(state.selectedWorld) || []).find((event) => event.id === eventId);
}

function formatEventTime(value) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) return value || "Unknown";
  return timestamp.toLocaleString([], { dateStyle: "medium", timeStyle: "long" });
}

function openEventDetails(item, { pointerActivated = false } = {}) {
  const event = eventById(item?.dataset.eventId);
  if (!event || elements.eventDialog.open) return;
  if (pointerActivated) item.blur();
  elements.commandInput.blur();
  elements.eventDialogWorld.textContent = event.world;
  const rows = eventDetailRows(event).map(([label, rawValue]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    if (label === "Time") {
      const time = document.createElement("time");
      time.dateTime = rawValue;
      time.textContent = formatEventTime(rawValue);
      detail.append(time);
    } else {
      detail.textContent = rawValue;
    }
    const row = document.createElement("div");
    row.append(term, detail);
    return row;
  });
  elements.eventDetails.replaceChildren(...rows);
  elements.eventDialog.showModal();
  scheduleViewportUpdate();
}

function renderTranscript({ scrollToLive = false, restorePosition = false } = {}) {
  if (liveRenderFrame !== null) window.cancelAnimationFrame(liveRenderFrame);
  liveRenderFrame = null;
  pendingLiveEvents = [];
  const events = state.events.get(state.selectedWorld) || [];
  const visible = events.slice(-MAX_RENDERED_EVENTS);
  const reading = readingState();
  elements.eventList.replaceChildren(...visible.map(eventNode));
  elements.emptyState.hidden = visible.length > 0;
  if (scrollToLive || reading.atLive) {
    requestAnimationFrame(() => {
      elements.transcript.scrollTop = elements.transcript.scrollHeight;
      reading.atLive = true;
      reading.unseenLive = 0;
      reading.scrollTop = elements.transcript.scrollTop;
      updateReturnLive();
    });
  } else if (restorePosition) {
    requestAnimationFrame(() => {
      elements.transcript.scrollTop = reading.scrollTop;
      updateReturnLive();
    });
  }
}

function flushLiveEvents() {
  liveRenderFrame = null;
  const events = pendingLiveEvents;
  pendingLiveEvents = [];
  const reading = readingState();
  const visible = events.filter((event) => event.world === state.selectedWorld);
  if (!visible.length) return;
  if (visible.length >= MAX_RENDERED_EVENTS) {
    elements.eventList.replaceChildren(...visible.slice(-MAX_RENDERED_EVENTS).map(eventNode));
  } else {
    const fragment = document.createDocumentFragment();
    for (const event of visible) fragment.append(eventNode(event));
    elements.eventList.append(fragment);
  }
  while (elements.eventList.children.length > MAX_RENDERED_EVENTS) {
    elements.eventList.firstElementChild?.remove();
  }
  elements.emptyState.hidden = true;
  if (reading.atLive) elements.transcript.scrollTop = elements.transcript.scrollHeight;
}

function appendLiveEvent(event) {
  pendingLiveEvents.push(event);
  if (liveRenderFrame === null) liveRenderFrame = requestAnimationFrame(flushLiveEvents);
}

function updateReturnLive() {
  const reading = readingState();
  elements.returnLive.hidden = reading.atLive || reading.unseenLive === 0;
  elements.returnLiveCount.textContent = reading.unseenLive > 0 ? String(reading.unseenLive) : "";
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
  const world = state.worlds.find((item) => item.world === event.world);
  if (world && generationIsNewer(event.connection_generation, world.connection_generation)) {
    world.connection_generation = event.connection_generation;
    world.history = {
      connection_generation: event.connection_generation,
      available_count: "0",
      snapshot_count: 0,
      snapshot_gap: false,
    };
    state.shownHistoryNotices.delete(event.world);
    if (event.world === state.selectedWorld && !state.historyReset) showHistoryNotice("");
  }
  if (state.ready) recordLiveHistoryEvent(world, event);
  events.push(event);
  eventIds.add(event.id);
  let evicted = false;
  if (events.length > MAX_EVENTS_PER_WORLD) {
    for (const removed of events.splice(0, events.length - MAX_EVENTS_PER_WORLD)) {
      eventIds.delete(removed.id);
      evicted = true;
    }
  }
  if (event.connection_state) {
    if (world) world.state = event.connection_state;
  }

  if (state.ready) {
    state.cursor = message.cursor;
  } else {
    return;
  }
  const reading = readingState(event.world);
  if (event.world !== state.selectedWorld) {
    state.unread.set(event.world, (state.unread.get(event.world) || 0) + 1);
    if (!reading.atLive) reading.unseenLive += 1;
    renderWorlds();
    return;
  }
  if (evicted) updateHistoryNotice();
  if (state.ready && !reading.atLive) {
    reading.unseenLive += 1;
    updateReturnLive();
  }
  if (reading.atLive) appendLiveEvent(event);
  if (event.connection_state) {
    updateWorldHeader();
    renderWorlds();
  }
}

function resetGatewayState() {
  state.events.clear();
  state.eventIds.clear();
  state.unread.clear();
  state.reading.clear();
  state.shownHistoryNotices.clear();
  state.cursor = null;
  safeRemove("tfr.cursor");
}

function discardEventsBeforeSnapshotGap(world) {
  const events = state.events.get(world.world) || [];
  if (!historySnapshotHasGap(world, events)) return;
  const retained = events.filter(
    (event) => event.connection_generation !== world.connection_generation,
  );
  state.events.set(world.world, retained);
  state.eventIds.set(world.world, new Set(retained.map((event) => event.id)));
}

function handleMessage(message) {
  if (!message || message.protocol !== 1 || typeof message.type !== "string") return;
  if (message.type === "hello") {
    state.ready = false;
    state.historyReset = message.history_reset === true;
    if (message.history_reset || (state.gatewayId && state.gatewayId !== message.gateway_id)) {
      resetGatewayState();
    }
    state.gatewayId = message.gateway_id;
    safeStore("tfr.gatewayId", state.gatewayId);
    const buildVersion = message.build?.version;
    const buildCommit = message.build?.commit;
    elements.gatewayVersion.textContent =
      typeof buildVersion === "string"
        ? `${buildVersion}${typeof buildCommit === "string" ? ` (${buildCommit.slice(0, 8)})` : ""}`
        : "Unknown";
    state.worlds = Array.isArray(message.worlds) ? message.worlds : [];
    for (const world of state.worlds) discardEventsBeforeSnapshotGap(world);
    if (!state.worlds.some((world) => world.world === state.selectedWorld)) {
      state.selectedWorld = state.worlds[0]?.world || null;
    }
    if (state.selectedWorld) {
      safeStore("tfr.selectedWorld", state.selectedWorld);
      elements.commandInput.value = state.drafts[state.selectedWorld] || "";
    }
    showHistoryNotice("");
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
    renderTranscript({ restorePosition: true });
    if (state.historyReset) {
      showHistoryNotice(
        "The Gateway restarted. Showing a fresh retained history.",
        6000,
        () => {
          state.historyReset = false;
          updateHistoryNotice();
        },
      );
    } else {
      updateHistoryNotice();
    }
    startHeartbeat();
    return;
  }
  if (message.type === "ack") {
    if (message.request_id === state.heartbeatRequestId) {
      clearHeartbeatTimeout();
      scheduleHeartbeat();
      return;
    }
    const pending = state.pending.get(message.request_id);
    state.pending.delete(message.request_id);
    if (message.ok !== true) {
      showToast(message.error || "Command was rejected", 4200);
      if (pending) {
        state.drafts[pending.world] = pending.text.slice(0, MAX_STORED_COMMAND_CHARACTERS);
        if (pending.world === state.selectedWorld) elements.commandInput.value = pending.text;
      }
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

function clearHeartbeatTimeout() {
  window.clearTimeout(state.heartbeatTimeout);
  state.heartbeatTimeout = null;
  state.heartbeatRequestId = null;
}

function stopHeartbeat() {
  window.clearTimeout(state.heartbeatTimer);
  state.heartbeatTimer = null;
  clearHeartbeatTimeout();
}

function scheduleHeartbeat(delay = HEARTBEAT_INTERVAL_MS) {
  window.clearTimeout(state.heartbeatTimer);
  state.heartbeatTimer = window.setTimeout(sendHeartbeat, delay);
}

function sendHeartbeat() {
  window.clearTimeout(state.heartbeatTimer);
  state.heartbeatTimer = null;
  if (document.visibilityState !== "visible") {
    scheduleHeartbeat();
    return;
  }
  const socket = state.socket;
  if (!state.ready || !socket || socket.readyState !== WebSocket.OPEN) return;
  clearHeartbeatTimeout();
  const requestId = crypto.randomUUID();
  state.heartbeatRequestId = requestId;
  try {
    socket.send(JSON.stringify({ type: "ping", request_id: requestId }));
  } catch {
    socket.close();
    return;
  }
  state.heartbeatTimeout = window.setTimeout(() => {
    if (state.socket === socket && state.heartbeatRequestId === requestId) {
      setConnection("Reconnecting", "error");
      socket.close();
    }
  }, HEARTBEAT_TIMEOUT_MS);
}

function startHeartbeat() {
  stopHeartbeat();
  scheduleHeartbeat();
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
    stopHeartbeat();
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
        "This device session expired or was revoked. Create a new link and paste it below.";
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
  const text = normalizeWorldCommand(elements.commandInput.value);
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
  window.requestAnimationFrame(() => {
    elements.commandInput.focus({ preventScroll: true });
    scheduleViewportUpdate();
  });
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
  stopHeartbeat();
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  state.socket?.close();
  resetGatewayState();
  state.worlds = [];
  state.gatewayId = null;
  state.selectedWorld = null;
  state.drafts = {};
  state.commandHistory = {};
  state.pending.clear();
  state.reading.clear();
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
  const fragmentCode = new URLSearchParams(location.hash.slice(1)).get("pair");
  if (fragmentCode) {
    storeSession("tfr.pairingCode", fragmentCode);
    storeSession("tfr.pairingAttempts", "0");
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }
  const pairingCode = fragmentCode || readSessionStorage("tfr.pairingCode");
  const pairingResult = new URLSearchParams(location.search).get("pairing");
  if (pairingResult) {
    history.replaceState(null, "", location.pathname);
    const redirectedSession = await sessionState();
    if (redirectedSession === "paired") {
      clearPairingState();
      elements.pairing.hidden = true;
      elements.console.hidden = false;
      connect();
      return;
    }
    if (pairingResult === "invalid") {
      clearPairingState();
      elements.pairing.hidden = false;
      elements.console.hidden = true;
      elements.pairingMessage.textContent =
        "Pairing code is invalid or expired. Create a new pairing link on the Gateway.";
      return;
    }
    if (pairingResult === "retry" && pairingCode) {
      postPairing(pairingCode, 5000);
      return;
    }
  }
  if (pairingCode) {
    postPairing(pairingCode);
    return;
  }
  const session = await sessionState();
  const paired = session === "paired";
  if (session === "unreachable") {
    elements.pairing.hidden = true;
    elements.console.hidden = false;
    setConnection("Offline", "error");
    scheduleReconnect();
    return;
  }
  elements.pairing.hidden = paired;
  elements.console.hidden = !paired;
  if (!paired) {
    await clearLocalData();
    elements.pairingMessage.textContent =
      'On the Gateway host, run tfr pair --device-name "My iPhone", then paste that link below.';
    return;
  }
  connect();
}

async function resume() {
  const session = await sessionState();
  if (session === "unreachable") return;
  const pairingCode = readSessionStorage("tfr.pairingCode");
  if (session === "paired") clearPairingState();
  if (session === "unpaired" && pairingCode) {
    postPairing(pairingCode);
    return;
  }
  if (session === "unpaired") {
    await clearLocalData();
    elements.console.hidden = true;
    elements.pairing.hidden = false;
    elements.pairingMessage.textContent =
      "This device is not paired. Create a new pairing link on the Gateway.";
    return;
  }
  if (state.socket?.readyState === WebSocket.OPEN && state.ready) {
    sendHeartbeat();
  } else {
    connect();
  }
}

function showWaitingServiceWorker(worker) {
  if (!worker) return;
  elements.updateNotice.hidden = false;
}

function openSettingsDialog(event) {
  if (elements.settingsDialog.open) return;
  event?.preventDefault();
  elements.commandInput.blur();
  elements.settingsDialog.showModal();
  scheduleViewportUpdate();
}

function watchServiceWorkerRegistration(registration) {
  state.serviceWorkerRegistration = registration;
  showWaitingServiceWorker(registration.waiting);
  registration.addEventListener("updatefound", () => {
    const worker = registration.installing;
    if (!worker) return;
    worker.addEventListener("statechange", () => {
      if (worker.state === "installed" && navigator.serviceWorker.controller) {
        showWaitingServiceWorker(worker);
      }
    });
  });
}

async function checkForApplicationUpdate() {
  try {
    await state.serviceWorkerRegistration?.update();
  } catch {
    // Update checks are best-effort while the Gateway or tailnet is unavailable.
  }
}

elements.settingsButton.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "touch") openSettingsDialog(event);
});
elements.settingsButton.addEventListener("click", openSettingsDialog);
elements.pairingForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const code = pairingCodeFromLink(elements.pairingLink.value.trim(), location.origin);
  elements.pairingLink.value = "";
  if (!code) {
    elements.pairingMessage.textContent =
      "That is not a valid pairing link for this TFR Gateway. Create a new link and try again.";
    return;
  }
  storeSession("tfr.pairingCode", code);
  storeSession("tfr.pairingAttempts", "0");
  postPairing(code);
});
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
  if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    elements.composer.requestSubmit(elements.sendButton);
  } else if (event.key === "ArrowUp" && elements.commandInput.selectionStart === 0) {
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
elements.commandInput.addEventListener("focus", scheduleViewportUpdate);
elements.commandInput.addEventListener("blur", scheduleViewportUpdate);
elements.historyButton.addEventListener("click", openHistoryDialog);
elements.historyDialog.addEventListener("close", () => elements.historyList.replaceChildren());
elements.textSmaller.addEventListener("click", () => applyTextSize(textSizePreference() - 1));
elements.textReset.addEventListener("click", () => applyTextSize(0));
elements.textLarger.addEventListener("click", () => applyTextSize(textSizePreference() + 1));
elements.motionPreference.addEventListener("change", () => {
  applyMotionPreference(elements.motionPreference.value);
});
elements.lineWrap.addEventListener("change", () => {
  applyLineWrap(elements.lineWrap.checked);
});
elements.applyUpdate.addEventListener("click", () => {
  const worker = state.serviceWorkerRegistration?.waiting;
  if (!worker) return;
  state.reloadingForUpdate = true;
  elements.applyUpdate.disabled = true;
  worker.postMessage({ type: "SKIP_WAITING" });
});
elements.returnLive.addEventListener("click", () => renderTranscript({ scrollToLive: true }));
elements.eventList.addEventListener("pointerdown", (event) => {
  setInputModality("pointer");
  transcriptPointerTarget = event.target.closest("a") ? null : event.target.closest(".event");
});
elements.eventList.addEventListener("pointercancel", () => {
  transcriptPointerTarget = null;
});
elements.eventList.addEventListener("click", (event) => {
  const item = event.target.closest(".event");
  const pointerActivated = item !== null && item === transcriptPointerTarget;
  transcriptPointerTarget = null;
  if (
    performance.now() < suppressTranscriptClickUntil ||
    event.target.closest("a") ||
    window.getSelection()?.toString()
  ) {
    return;
  }
  openEventDetails(item, { pointerActivated });
});
elements.eventList.addEventListener("keydown", (event) => {
  setInputModality("keyboard");
  if ((event.key !== "Enter" && event.key !== " ") || event.target.closest("a")) return;
  const item = event.target.closest(".event");
  if (!item) return;
  event.preventDefault();
  openEventDetails(item);
});
elements.transcript.addEventListener(
  "touchstart",
  (event) => {
    setInputModality("pointer");
    if (document.activeElement?.classList?.contains("event")) document.activeElement.blur();
    if (event.touches.length !== 1 || event.target.closest?.("a")) {
      transcriptTouch = null;
      return;
    }
    const touch = event.touches[0];
    transcriptTouch = { x: touch.clientX, y: touch.clientY, time: performance.now() };
  },
  { passive: true },
);
elements.transcript.addEventListener(
  "touchend",
  (event) => {
    const start = transcriptTouch;
    transcriptTouch = null;
    if (!start || event.changedTouches.length !== 1 || window.getSelection()?.toString()) return;
    const touch = event.changedTouches[0];
    const direction = swipeDirection(start, {
      x: touch.clientX,
      y: touch.clientY,
      time: performance.now(),
    });
    const moved = Math.hypot(touch.clientX - start.x, touch.clientY - start.y) > 10;
    if (moved) suppressTranscriptClickUntil = performance.now() + 500;
    if (direction && lineWrapPreference()) {
      switchWorld(direction);
    }
  },
  { passive: true },
);
document.addEventListener("keydown", () => setInputModality("keyboard"), { capture: true });
document.addEventListener("pointerdown", () => setInputModality("pointer"), { capture: true });
elements.transcript.addEventListener("touchcancel", () => {
  transcriptTouch = null;
});
elements.transcript.addEventListener("scroll", () => {
  const distance = elements.transcript.scrollHeight - elements.transcript.scrollTop - elements.transcript.clientHeight;
  const reading = readingState();
  const wasAtLive = reading.atLive;
  reading.atLive = distance < 36;
  reading.scrollTop = elements.transcript.scrollTop;
  if (!wasAtLive && reading.atLive && reading.unseenLive > 0) {
    renderTranscript({ scrollToLive: true });
    return;
  }
  if (reading.atLive) reading.unseenLive = 0;
  updateReturnLive();
});
window.addEventListener("online", () => {
  resume();
  checkForApplicationUpdate();
});
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  window.clearTimeout(pairingRecoveryTimer);
  pairingSubmission.reset();
  resume();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    scheduleViewportUpdate();
    resume();
    checkForApplicationUpdate();
  }
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (state.reloadingForUpdate) location.reload();
  });
  window.addEventListener("load", async () => {
    try {
      watchServiceWorkerRegistration(await navigator.serviceWorker.register("/sw.js"));
    } catch {
      // The live app remains usable when service-worker registration is unavailable.
    }
  });
}

start();
