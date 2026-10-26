import {
  createPairingSubmission,
  pairingCodeFromLink,
  shouldShowInstallOnboarding,
  submitPairing,
} from "./pairing.mjs";
import {
  commandWithAutomaticSay,
  isMultilineWorldCommand,
  normalizeWorldCommand,
} from "./command.mjs";
import {
  fireworkParticleBudget,
  observeCombo,
  validatedComboCue,
} from "./combo.mjs";
import { createConnectionLifecycle } from "./connection-lifecycle.mjs";
import {
  clearDiagnostics,
  diagnosticEntries,
  recordDiagnostic,
  reportDiagnostic,
} from "./diagnostics.mjs";
import { connectionNotice, eventDetailRows } from "./event-details.mjs";
import {
  comboLabDefinition,
  parseEffectsLabCommand,
  validatedEffectDemos,
} from "./effects-lab.mjs";
import {
  generationIsNewer,
  historyNoticeDecision,
  historySnapshotHasGap,
  recordLiveHistoryEvent,
} from "./history-notice.mjs";
import { linkParts } from "./linkify.mjs";
import { motionAllowsAnimation } from "./motion.mjs";
import { preparePastePreview, validatedPasteUpdate } from "./paste.mjs";
import {
  animatePresentationElement,
  splitPresentationRuns,
  presentationValidationResult,
} from "./presentation.mjs";
import { restoreTranscriptPosition } from "./scroll-restoration.mjs";
import {
  animateScreenClear,
  captureTranscriptCells,
  eventsAfterClearCutoff,
  isRestoreSwipe,
  selectScreenClearEffect,
  validatedScreenClearConfig,
} from "./screen-clear.mjs";
import { swipeDirection } from "./swipe.mjs";
import { installTextRunColors, textRunClassNames, validatedTextRuns } from "./text-runs.mjs";
import { isTranscriptEvent } from "./transcript-events.mjs";

const MAX_EVENTS_PER_WORLD = 2500;
const MAX_RENDERED_EVENTS = 500;
const MAX_ANIMATED_PRESENTATIONS = 64;
const CHARACTER_SWEEP_ANIMATION_COST = 4;
const MAX_COMMAND_HISTORY = 50;
const MAX_STORED_COMMAND_CHARACTERS = 4096;
const MAX_PAIRING_ATTEMPTS = 3;
const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000, 15000];
const HEARTBEAT_INTERVAL_MS = 20000;
const HEARTBEAT_TIMEOUT_MS = 8000;
const CONNECTION_TIMEOUT_MS = 8000;
const EVENT_LOOP_PROBE_MS = 2000;
const EVENT_LOOP_STALL_MS = 1000;
const RESPONSIVENESS_REPORT_COOLDOWN_MS = 30000;
const INTERACTION_ACK_TIMEOUT_MS = 1500;
const SLOW_TRANSCRIPT_RENDER_MS = 250;
const PRESENTATION_CHARACTER_PRESSURE = 2000;
const TRANSCRIPT_NODE_PRESSURE = 10000;
const MIN_TEXT_SIZE = -2;
const MAX_TEXT_SIZE = 4;
const SCREEN_CLEAR_PULL_DISTANCE = 96;

const elements = {
  pairing: document.querySelector("#pairing"),
  pairingMessage: document.querySelector("#pairing-message"),
  pairingForm: document.querySelector("#pairing-form"),
  pairingLink: document.querySelector("#pairing-link"),
  console: document.querySelector("#console"),
  settingsButton: document.querySelector("#settings-button"),
  worldName: document.querySelector("#world-name"),
  worldSignal: document.querySelector("#world-signal"),
  worldActivity: document.querySelector("#world-activity"),
  connectionState: document.querySelector("#connection-state"),
  historyNotice: document.querySelector("#history-notice"),
  transcript: document.querySelector("#transcript"),
  eventList: document.querySelector("#event-list"),
  emptyState: document.querySelector("#empty-state"),
  screenClearOverlay: document.querySelector("#screen-clear-overlay"),
  screenClearHandle: document.querySelector("#screen-clear-handle"),
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
  eventSheetBody: document.querySelector("#event-dialog .event-sheet-body"),
  eventDetails: document.querySelector("#event-details"),
  textSmaller: document.querySelector("#text-smaller"),
  textReset: document.querySelector("#text-reset"),
  textLarger: document.querySelector("#text-larger"),
  motionPreference: document.querySelector("#motion-preference"),
  lineWrap: document.querySelector("#line-wrap"),
  spellingSuggestions: document.querySelector("#spelling-suggestions"),
  automaticSay: document.querySelector("#automatic-say"),
  unpairDevice: document.querySelector("#unpair-device"),
  installOnboarding: document.querySelector("#install-onboarding"),
  continueInBrowser: document.querySelector("#continue-in-browser"),
  uninstallDevice: document.querySelector("#uninstall-device"),
  uninstallDialog: document.querySelector("#uninstall-dialog"),
  updateNotice: document.querySelector("#update-notice"),
  applyUpdate: document.querySelector("#apply-update"),
  toast: document.querySelector("#toast"),
  comboNotice: document.querySelector("#combo-notice"),
  comboFireworks: document.querySelector("#combo-fireworks"),
  effectsLabButton: document.querySelector("#effects-lab-button"),
  effectsLab: document.querySelector("#effects-lab"),
  effectsLabClose: document.querySelector("#effects-lab-close"),
  effectsLabForceMotion: document.querySelector("#effects-lab-force-motion"),
  effectsLabCombos: document.querySelector("#effects-lab-combos"),
  effectsLabSpeakers: document.querySelector("#effects-lab-speakers"),
  effectsLabStage: document.querySelector("#effects-lab-stage"),
  effectsLabMessage: document.querySelector("#effects-lab-message"),
  effectsLabNotice: document.querySelector("#effects-lab-notice"),
  effectsLabFireworks: document.querySelector("#effects-lab-fireworks"),
  effectsLabCommand: document.querySelector("#effects-lab-command"),
  effectsLabCommandInput: document.querySelector("#effects-lab-command-input"),
  diagnosticsButton: document.querySelector("#diagnostics-button"),
  diagnosticsDialog: document.querySelector("#diagnostics-dialog"),
  diagnosticsOutput: document.querySelector("#diagnostics-output"),
  diagnosticsCopy: document.querySelector("#diagnostics-copy"),
  diagnosticsClear: document.querySelector("#diagnostics-clear"),
  diagnosticsClose: document.querySelector("#diagnostics-close"),
  pasteDialog: document.querySelector("#paste-dialog"),
  pasteDialogWorld: document.querySelector("#paste-dialog-world"),
  pasteSummary: document.querySelector("#paste-summary"),
  pastePreview: document.querySelector("#paste-preview"),
  pastePreviewNote: document.querySelector("#paste-preview-note"),
  pasteStatus: document.querySelector("#paste-status"),
  pasteCancel: document.querySelector("#paste-cancel"),
  pasteStop: document.querySelector("#paste-stop"),
  pasteConfirm: document.querySelector("#paste-confirm"),
};

const state = {
  historyReset: false,
  gatewayId: readStorage("tfr.gatewayId"),
  cursor: null,
  worlds: [],
  events: new Map(),
  eventIds: new Map(),
  unread: new Map(),
  reading: new Map(),
  selectedWorld: readStorage("tfr.selectedWorld"),
  drafts: new Map(),
  commandHistory: new Map(),
  historyIndex: null,
  pending: new Map(),
  toastTimer: null,
  heartbeatTimer: null,
  heartbeatTimeout: null,
  heartbeatRequestId: null,
  serviceWorkerRegistration: null,
  reloadingForUpdate: false,
  shownHistoryNotices: new Map(),
  comboStreaks: new Map(),
  speakingStreaksEnabled: true,
  effectDemos: [],
  screenClearConfig: validatedScreenClearConfig(null),
  screenClearCutoffs: new Map(),
  lastScreenClearEffects: new Map(),
  paste: null,
};
const pairingSubmission = createPairingSubmission(
  (code) => submitPairing(document, code),
  (callback, delay) => window.setTimeout(callback, delay),
);
let pairingRecoveryTimer = null;
let viewportTimer = null;
let historyNoticeTimer = null;
let historyNoticeKey = null;
let historyDialogPending = false;
let pendingInteraction = null;
let interactionSequence = 0;
const lastResponsivenessReports = new Map();
let transcriptTouch = null;
let suppressTranscriptClickUntil = 0;
let transcriptPointerTarget = null;
let pendingLiveEvents = [];
let liveRenderFrame = null;
let preserveLiveDuringViewportChange = false;
const installedTextRunColors = new Set();
const activePresentationAnimations = new Set();
let comboNoticeAnimation = null;
const comboTimers = new Set();
let activeScreenClearAnimation = null;
let screenClearPull = null;
let screenClearSeed = 0;

function responsivenessMessage(prefix) {
  const events = elements.eventList?.children.length || 0;
  const characters = elements.eventList?.querySelectorAll(".presentation-character").length || 0;
  return `${prefix}; events=${events}; characters=${characters}; animations=${activePresentationAnimations.size}`;
}

function reportResponsiveness(code, message, operation) {
  const now = performance.now();
  if (now - (lastResponsivenessReports.get(code) ?? -Infinity) < RESPONSIVENESS_REPORT_COOLDOWN_MS) return;
  lastResponsivenessReports.set(code, now);
  reportDiagnostic(recordDiagnostic({
    severity: "warning",
    component: "responsiveness",
    code,
    message: responsivenessMessage(message),
    operation,
  }));
}

function interactionOperation(target) {
  if (!(target instanceof Element)) return null;
  const control = target.closest("button, input, textarea, select, a, [role='button']");
  if (!control) return null;
  return (control.id || control.getAttribute("aria-label") || control.tagName).slice(0, 100);
}

function acknowledgeInteraction(target = null) {
  if (!pendingInteraction) return;
  if (target && pendingInteraction.target !== target) return;
  pendingInteraction = null;
}

function monitorEventLoop(expected = performance.now() + EVENT_LOOP_PROBE_MS) {
  window.setTimeout(() => {
    const now = performance.now();
    const delay = now - expected;
    if (delay >= EVENT_LOOP_STALL_MS && document.visibilityState === "visible") {
      reportResponsiveness(
        "main_thread_stall",
        `Main thread probe was delayed ${Math.round(delay)} ms`,
        document.visibilityState,
      );
    }
    monitorEventLoop(now + EVENT_LOOP_PROBE_MS);
  }, EVENT_LOOP_PROBE_MS);
}

document.addEventListener("pointerdown", (event) => {
  const operation = interactionOperation(event.target);
  if (!operation) return;
  const control = event.target instanceof Element
    ? event.target.closest("button, input, textarea, select, a, [role='button']")
    : null;
  if (!control) return;
  const sequence = ++interactionSequence;
  pendingInteraction = { sequence, target: control };
  window.setTimeout(() => {
    if (pendingInteraction?.sequence !== sequence) return;
    pendingInteraction = null;
    reportResponsiveness(
      "interaction_unacknowledged",
      `Pointer interaction produced no click or focus within ${INTERACTION_ACK_TIMEOUT_MS} ms`,
      operation,
    );
  }, INTERACTION_ACK_TIMEOUT_MS);
}, true);
document.addEventListener("click", () => acknowledgeInteraction(), true);
document.addEventListener("focusin", () => acknowledgeInteraction(), true);
monitorEventLoop();

function scheduleCombo(callback, delay) {
  const timer = window.setTimeout(() => {
    comboTimers.delete(timer);
    callback();
  }, delay);
  comboTimers.add(timer);
  return timer;
}

function cancelComboVisuals() {
  for (const timer of comboTimers) window.clearTimeout(timer);
  comboTimers.clear();
  for (const node of document.querySelectorAll(".combo-text, .combo-notice, .combo-fireworks i")) {
    for (const animation of node.getAnimations()) animation.cancel();
  }
  elements.comboNotice.hidden = true;
  elements.comboFireworks.replaceChildren();
}

function comboTextNodes(root, start, end) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const selected = [];
  let offset = 0;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const next = offset + Array.from(node.data).length;
    if (start < next && end > offset) selected.push({ node, start: Math.max(0, start - offset), end: Math.min(next, end) - offset });
    offset = next;
  }
  return selected;
}

function codeUnitOffset(text, scalarOffset) {
  return Array.from(text).slice(0, scalarOffset).join("").length;
}

function animateComboText(root, combo) {
  if (!combo || combo.count > 7) return;
  const wrappers = [];
  for (const part of comboTextNodes(root, combo.body_start, combo.body_end).toReversed()) {
    const range = document.createRange();
    range.setStart(part.node, codeUnitOffset(part.node.data, part.start));
    range.setEnd(part.node, codeUnitOffset(part.node.data, part.end));
    const wrapper = document.createElement("span");
    wrapper.className = `combo-text combo-${combo.count}`;
    range.surroundContents(wrapper);
    wrappers.push(wrapper);
  }
  for (const wrapper of wrappers) {
    if (combo.count === 3) wrapper.animate([{ fontWeight: 800 }, { fontWeight: 800 }], { duration: 250 });
    if (combo.count === 4) wrapper.animate([{ color: "inherit" }, { color: "#ffffff" }, { color: "inherit" }, { color: "#ffffff" }, { color: "inherit" }], { duration: 1000 });
    if (combo.count === 5) {
      wrapper.animate([{ color: "inherit" }, { color: "#ffffff" }], { duration: 200, iterations: 10, direction: "alternate", easing: "steps(1)" });
      scheduleCombo(() => wrapper.classList.remove("combo-5"), 2000);
    }
    if (combo.count === 6) {
      const characters = Array.from(wrapper.textContent || "");
      const spans = characters.map((character) => {
        const span = document.createElement("span");
        span.textContent = character;
        return span;
      });
      wrapper.replaceChildren(...spans);
      spans.forEach((span, index) => {
        const frames = [];
        for (let frame = 0; frame <= 40; frame += 1) {
          const progress = frame / 40;
          if (progress >= 0.5) {
            frames.push({ color: progress === 0.5 ? "#ffffff" : "inherit", offset: progress });
            continue;
          }
          const sweep = progress * 8;
          const fraction = sweep % 1;
          const position = (Math.floor(sweep) % 2 === 0 ? fraction : 1 - fraction) * Math.max(0, spans.length - 1);
          frames.push({ color: Math.abs(index - position) <= 1 ? "#ffffff" : "inherit", offset: progress });
        }
        span.animate(frames, { duration: 4000, easing: "linear" });
      });
    }
    if (combo.count === 7) wrapper.animate([{ color: "inherit" }, { color: "#ffffff" }, { color: "inherit" }, { color: "#ffffff" }, { color: "inherit" }], { duration: 2000 });
  }
}

function launchFireworks(eventId, stage = elements.comboFireworks) {
  let seed = [...eventId].reduce((value, character) => (value * 33 + character.codePointAt(0)) >>> 0, 5381);
  const random = () => ((seed = (1664525 * seed + 1013904223) >>> 0) / 2 ** 32);
  const colors = ["#ff0000", "#ff8000", "#0070dd", "#ffff00", "#a335ee"];
  const styles = getComputedStyle(stage);
  const cellWidth = Math.max(6, Number.parseFloat(styles.fontSize) * 0.62 || 8);
  const lineHeight = Math.max(10, Number.parseFloat(styles.lineHeight) || 16);
  const budget = fireworkParticleBudget(stage.clientWidth, stage.clientHeight, cellWidth, lineHeight);
  const count = 1 + Math.floor(random() * 3);
  const particlesPerBurst = Math.floor(budget / count);
  const particleRemainder = budget % count;
  for (let firework = 0; firework < count; firework += 1) {
    const x = 10 + random() * 80;
    const y = 10 + random() * 55;
    const delay = random() * 1800;
    scheduleCombo(() => {
      const burstBudget = particlesPerBurst + (firework < particleRemainder ? 1 : 0);
      for (let index = 0; index < burstBudget; index += 1) {
        const particle = document.createElement("i");
        particle.style.setProperty("--x", `${x}%`);
        particle.style.setProperty("--y", `${y}%`);
        particle.style.setProperty("--dx", `${(random() - 0.5) * 9}rem`);
        particle.style.setProperty("--dy", `${(random() - 0.7) * 8}rem`);
        particle.style.background = colors[Math.floor(random() * colors.length)];
        stage.append(particle);
        particle.addEventListener("animationend", () => particle.remove(), { once: true });
      }
    }, delay);
  }
}

function comboNoticeFrames(count) {
  if (count === 3) {
    return [
      { transform: "scaleX(0)", opacity: 0 },
      { transform: "scaleX(1.08)", opacity: 1, offset: 0.12 },
      { transform: "scaleX(1)", opacity: 1, offset: 0.82 },
      { filter: "invert(1)", opacity: 1, offset: 0.9 },
      { opacity: 0 },
    ];
  }
  if (count === 4) {
    return [
      { transform: "translateX(-1rem)", opacity: 0 },
      { transform: "translateX(.25rem)", opacity: 1, offset: 0.12 },
      { transform: "translateX(-.25rem)", offset: 0.18 },
      { transform: "translateX(0)", opacity: 1, offset: 0.84 },
      { filter: "invert(1)", offset: 0.92 },
      { opacity: 0 },
    ];
  }
  if (count === 5) {
    return [
      { letterSpacing: "-.25em", filter: "blur(3px)", opacity: 0 },
      { letterSpacing: ".16em", filter: "blur(0)", opacity: 1, offset: 0.18 },
      { letterSpacing: "normal", opacity: 1, offset: 0.84 },
      { filter: "invert(1)", offset: 0.92 },
      { opacity: 0 },
    ];
  }
  if (count === 6) {
    return [
      { transform: "translateX(-45vw)", opacity: 1 },
      { transform: "translateX(45vw)", offset: 0.14 },
      { transform: "translateX(-20vw)", offset: 0.25 },
      { transform: "scale(1.28)", filter: "invert(1)", offset: 0.34 },
      { transform: "scale(1)", filter: "invert(0)", opacity: 1, offset: 0.84 },
      { filter: "invert(1)", offset: 0.92 },
      { opacity: 0 },
    ];
  }
  if (count === 7) {
    return [
      { transform: "scale(.7) rotate(-3deg)", opacity: 0 },
      { transform: "scale(1.25) rotate(3deg)", opacity: 1, offset: 0.15 },
      { transform: "scale(1.1) rotate(0)", opacity: 1, offset: 0.7 },
      { transform: "translateY(-2rem) scale(1.2)", opacity: 0 },
    ];
  }
  return [{ opacity: 1 }, { opacity: 1, offset: 0.84 }, { filter: "invert(1)", offset: 0.92 }, { opacity: 0 }];
}

function labMotionEnabled() {
  return elements.effectsLabForceMotion.checked || motionAllowsAnimation(
    elements.motionPreference.value,
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
}

function previewLabCombo(count) {
  const definition = comboLabDefinition(count);
  if (!definition) return;
  const { notice, color } = definition;
  elements.effectsLabMessage.textContent = `Demo says, "This triggers combo level ${count}."`;
  elements.effectsLabNotice.textContent = notice;
  elements.effectsLabNotice.style.color = color;
  elements.effectsLabNotice.hidden = false;
  if (!labMotionEnabled()) return;
  const combo = {
    count,
    body_start: 12,
    body_end: Array.from(elements.effectsLabMessage.textContent).length - 1,
  };
  animateComboText(elements.effectsLabMessage, combo);
  elements.effectsLabNotice.animate(
    comboNoticeFrames(count),
    { duration: 3600 },
  );
  if (count === 7) launchFireworks(`effects-lab-${count}`, elements.effectsLabFireworks);
}

function previewLabSpeaker(demo) {
  const sample = demo?.samples?.[0];
  if (!sample) return;
  const validation = presentationValidationResult(sample);
  const hasPresentation = validation.value !== null;
  elements.effectsLabMessage.replaceChildren();
  appendEventText(
    elements.effectsLabMessage,
    { ...sample, timestamp: new Date().toISOString() },
    { animate: labMotionEnabled(), animationsEnabled: labMotionEnabled() },
  );
  elements.effectsLabNotice.textContent = hasPresentation
    ? ""
    : sample.presentation
      ? `This PWA rejected the configured sample's portable presentation: ${validation.error.code} at ${validation.error.path}.`
      : "The Gateway did not provide a portable presentation for this configured sample.";
  elements.effectsLabNotice.hidden = hasPresentation;
  if (!hasPresentation) {
    const entry = recordDiagnostic({
      severity: "warning",
      component: "presentation",
      code: sample.presentation ? validation.error.code : "gateway_omitted",
      message: sample.presentation
        ? `The PWA rejected a configured presentation (${validation.error.code})`
        : "The Gateway omitted a configured Effects Lab presentation",
      operation: demo.id,
      event_id: sample.id,
    });
    reportDiagnostic(entry);
  } else {
    scheduleLabPresentationTrace(demo, validation.value);
  }
}

function presentationTrack(program) {
  const variant = program?.variants?.[0];
  if (!variant) return "style";
  for (const [field, name] of [
    ["character_palette_front", "palette_front"],
    ["character_hue_wave", "hue_wave"],
    ["character_sweep", "sweep"],
    ["character_style_sweep", "style_sweep"],
    ["character_sparkle", "sparkle"],
    ["character_reveal", "reveal"],
  ]) {
    if (variant[field]) return name;
  }
  return variant.foreground_keyframes?.length ? "foreground_timeline" : "style";
}

function recordLabPresentationTrace(demo, programs, phase) {
  const characters = [...elements.effectsLabMessage.querySelectorAll(".presentation-character")];
  const colors = characters.slice(0, 5).map((node) => getComputedStyle(node).color);
  const inline = characters.slice(0, 5).map((node) => node.style.color || "-");
  const visual = characters.slice(0, 5).map((node) => node.dataset.visualText ? "v" : "-").join("");
  const clocks = [...elements.effectsLabMessage.querySelectorAll(".presentation-target")]
    .flatMap((node) => node.getAnimations())
    .map((animation) => `${animation.playState}:${Math.round(Number(animation.currentTime) || 0)}`);
  const message = [
    `phase=${phase}`,
    `tracks=${programs.map(presentationTrack).join("+")}`,
    `targets=${characters.length}`,
    `clocks=${clocks.join(",") || "none"}`,
    `computed=${colors.join(",") || "none"}`,
    `inline=${inline.join(",") || "none"}`,
    `visual=${visual || "none"}`,
  ].join("; ").slice(0, 240);
  reportDiagnostic(recordDiagnostic({
    severity: "info",
    component: "presentation",
    code: "lab_presentation_trace",
    message,
    operation: `${demo.id}:${phase}`.slice(0, 100),
  }));
}

function scheduleLabPresentationTrace(demo, programs) {
  const primary = programs.find((program) =>
    !program.variants.some((variant) => variant.character_reveal)
  ) || programs[0];
  const reveal = programs.find((program) =>
    program.variants.some((variant) => variant.character_reveal)
  );
  const checkpoints = new Map([
    [100, "start"],
    [Math.max(150, Math.round(primary.duration_ms / 2)), "active"],
    [primary.duration_ms + 100, "idle"],
  ]);
  if (reveal) checkpoints.set(reveal.duration_ms + 100, "after-reveal");
  for (const [delay, phase] of checkpoints) {
    window.setTimeout(() => recordLabPresentationTrace(demo, programs, phase), delay);
  }
}

function renderEffectsLabCatalog() {
  elements.effectsLabCombos.replaceChildren();
  for (let count = 3; count <= 8; count += 1) {
    const definition = comboLabDefinition(count);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = definition.label;
    button.addEventListener("click", () => previewLabCombo(count));
    elements.effectsLabCombos.append(button);
  }
  elements.effectsLabSpeakers.replaceChildren();
  for (const demo of state.effectDemos) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = demo.label;
    button.addEventListener("click", () => previewLabSpeaker(demo));
    elements.effectsLabSpeakers.append(button);
  }
  if (!state.effectDemos.length) elements.effectsLabSpeakers.textContent = "No configured speaker demos registered.";
}

function showCombo(event, node) {
  const combo = event.combo;
  if (!combo || combo.presented) return;
  combo.presented = true;
  if (!motionAllowsAnimation(elements.motionPreference.value, window.matchMedia("(prefers-reduced-motion: reduce)").matches)) return;
  if (node) animateComboText(node.querySelector(".event-text"), combo);
  comboNoticeAnimation?.cancel();
  elements.comboNotice.hidden = false;
  elements.comboNotice.textContent = combo.notice;
  elements.comboNotice.style.color = combo.color;
  comboNoticeAnimation = elements.comboNotice.animate(
    comboNoticeFrames(combo.count),
    { duration: 3600 },
  );
  comboNoticeAnimation.addEventListener("finish", () => { elements.comboNotice.hidden = true; }, { once: true });
  if (combo.count === 7) launchFireworks(event.id);
}

function cancelPresentationAnimations() {
  for (const animation of activePresentationAnimations) animation.cancel();
  activePresentationAnimations.clear();
}

function presentationAnimationCost(node) {
  if (!node.querySelector(".presentation-animated")) return 0;
  return node.querySelector(".presentation-character") ? CHARACTER_SWEEP_ANIMATION_COST : 1;
}

function eventPresentationAnimationCost(event) {
  return event?.presentation?.programs?.some((program) =>
    program.variants?.some((variant) =>
       variant.character_sweep
       || variant.character_hue_wave
       || variant.character_palette_front
       || variant.character_style_sweep
       || variant.character_sparkle
       || variant.character_reveal
    ),
  )
    ? CHARACTER_SWEEP_ANIMATION_COST
    : 1;
}

const connection = createConnectionLifecycle({
  connectionTimeout: CONNECTION_TIMEOUT_MS,
  reconnectDelays: RECONNECT_DELAYS,
  setTimer: (callback, delay) => window.setTimeout(callback, delay),
  clearTimer: (timer) => window.clearTimeout(timer),
  onConnectionTimeout: (socket) => {
    reportDiagnostic(recordDiagnostic({
      severity: "error",
      component: "connection",
      code: "ready_timeout",
      message: "Gateway connection did not become ready within eight seconds",
      operation: "websocket",
    }));
    setConnection(navigator.onLine ? "Gateway unavailable" : "Offline", "error");
    updateWorldHeader();
    try {
      socket.close();
    } catch {
      socket.addEventListener("open", () => socket.close(), { once: true });
    }
  },
  onReconnect: () => connect(),
});

function updateViewportHeight() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight;
  const inputFocused = document.activeElement === elements.commandInput;
  const offsetTop = inputFocused ? viewport?.offsetTop || 0 : 0;
  document.documentElement.toggleAttribute("data-input-focused", inputFocused);
  document.documentElement.style.setProperty("--viewport-height", `${height}px`);
  document.documentElement.style.setProperty("--viewport-offset-top", `${offsetTop}px`);
  if (inputFocused && preserveLiveDuringViewportChange) {
    window.requestAnimationFrame(() => {
      const reading = readingState();
      elements.transcript.scrollTop = elements.transcript.scrollHeight;
      reading.atLive = true;
      reading.scrollTop = elements.transcript.scrollTop;
      reading.unseenLive = 0;
      updateReturnLive();
    });
  }
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
window.addEventListener("orientationchange", () => {
  scheduleViewportUpdate();
  window.setTimeout(scheduleViewportUpdate, 750);
});
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

function spellingSuggestionsPreference() {
  return readStorage("tfr.spellingSuggestions") !== "off";
}

function applySpellingSuggestions(enabled) {
  elements.spellingSuggestions.checked = enabled;
  elements.commandInput.spellcheck = enabled;
  elements.commandInput.setAttribute("autocorrect", enabled ? "on" : "off");
  if (enabled) {
    safeRemove("tfr.spellingSuggestions");
  } else {
    safeStore("tfr.spellingSuggestions", "off");
  }
}

function automaticSayPreference() {
  return readStorage("tfr.automaticSay") === "on";
}

function applyAutomaticSay(enabled) {
  elements.automaticSay.checked = enabled;
  if (enabled) {
    safeStore("tfr.automaticSay", "on");
  } else {
    safeRemove("tfr.automaticSay");
  }
}

applyTextSize(textSizePreference());
applyMotionPreference(readStorage("tfr.motion") || "system");
applyLineWrap(lineWrapPreference());
applySpellingSuggestions(spellingSuggestionsPreference());
applyAutomaticSay(automaticSayPreference());

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

function renderPasteDialog() {
  const paste = state.paste;
  if (!paste) return;
  const { preview } = paste;
  elements.pasteDialogWorld.textContent = preview.world;
  elements.pasteSummary.textContent = `${preview.lineCount} line${preview.lineCount === 1 ? "" : "s"} · ${preview.byteCount} UTF-8 bytes`;
  elements.pastePreview.textContent = preview.preview;
  elements.pastePreviewNote.hidden = !preview.previewTruncated;
  elements.pasteCancel.hidden = paste.status === "active" || paste.status === "stopping";
  elements.pasteStop.hidden = paste.status !== "active";
  elements.pasteStop.disabled = paste.status !== "active";
  elements.pasteConfirm.hidden = ["active", "stopping", "complete"].includes(paste.status);
  elements.pasteConfirm.textContent = ["cancelled", "interrupted", "rejected"].includes(paste.status)
    ? "Retry transfer"
    : "Send pasted lines";
  if (paste.status === "preview") {
    elements.pasteStatus.textContent = "Each line will be sent as literal output, not executed as a pasted command.";
  } else if (paste.status === "active") {
    elements.pasteStatus.textContent = `Sending line ${Math.min(paste.sentLines + 1, paste.totalLines)} of ${paste.totalLines}.`;
  } else if (paste.status === "stopping") {
    elements.pasteStatus.textContent = `Stopping after ${paste.sentLines} accepted line${paste.sentLines === 1 ? "" : "s"}…`;
  } else if (paste.status === "complete") {
    elements.pasteStatus.textContent = `The Gateway accepted all ${paste.totalLines} lines.`;
    elements.pasteCancel.hidden = false;
    elements.pasteCancel.textContent = "Close";
  } else if (paste.status === "rejected") {
    elements.pasteStatus.textContent = paste.message || "The Gateway rejected this transfer.";
  } else {
    elements.pasteStatus.textContent = `${paste.sentLines} of ${paste.totalLines || preview.lineCount} lines were accepted before the transfer stopped. Retrying may duplicate them.`;
  }
}

function openPastePreview(text) {
  const world = currentWorld();
  if (!world || state.paste?.status === "active" || state.paste?.status === "stopping") return;
  try {
    state.paste = {
      preview: preparePastePreview(text, world.world),
      status: "preview",
      requestId: null,
      sentLines: 0,
      totalLines: 0,
    };
  } catch (error) {
    showToast(error instanceof Error ? error.message : "Pasted text is invalid", 4200);
    return;
  }
  elements.commandInput.blur();
  elements.pasteCancel.textContent = "Cancel";
  renderPasteDialog();
  if (!elements.pasteDialog.open) elements.pasteDialog.showModal();
}

function startPasteTransfer() {
  const paste = state.paste;
  if (!paste || ["active", "stopping", "complete"].includes(paste.status)) return;
  if (!connection.ready || !connection.socket || connection.socket.readyState !== WebSocket.OPEN) {
    paste.status = "interrupted";
    paste.message = "Gateway is not ready.";
    renderPasteDialog();
    return;
  }
  paste.requestId = crypto.randomUUID();
  paste.status = "active";
  paste.sentLines = 0;
  paste.totalLines = paste.preview.lineCount;
  paste.message = null;
  updateWorldHeader();
  renderPasteDialog();
  try {
    connection.socket.send(JSON.stringify({
      type: "paste",
      request_id: paste.requestId,
      world: paste.preview.world,
      text: paste.preview.text,
    }));
  } catch {
    paste.status = "interrupted";
    updateWorldHeader();
    renderPasteDialog();
  }
}

function stopPasteTransfer() {
  const paste = state.paste;
  if (!paste || paste.status !== "active" || !connection.socket) return;
  paste.status = "stopping";
  renderPasteDialog();
  try {
    connection.socket.send(JSON.stringify({ type: "cancel_paste", request_id: paste.requestId }));
  } catch {
    paste.status = "interrupted";
    updateWorldHeader();
    renderPasteDialog();
  }
}

function handlePasteUpdate(message) {
  const paste = state.paste;
  if (!paste?.requestId) return false;
  const update = validatedPasteUpdate(message, paste.requestId);
  if (!update) return false;
  paste.sentLines = update.sentLines;
  paste.totalLines = update.totalLines;
  if (update.type === "paste_complete") {
    paste.status = "complete";
  } else if (update.type === "paste_cancelled") {
    paste.status = update.reason === "disconnected" ? "interrupted" : "cancelled";
  } else {
    paste.status = "active";
  }
  updateWorldHeader();
  renderPasteDialog();
  return true;
}

function interruptPasteTransfer() {
  const paste = state.paste;
  if (!paste || !["active", "stopping"].includes(paste.status)) return;
  paste.status = "interrupted";
  updateWorldHeader();
  renderPasteDialog();
}

function updateWorldHeader() {
  const world = currentWorld();
  elements.worldName.textContent = world?.world || "Select world";
  elements.worldSignal.className = `world-signal ${world?.state || ""}`.trim();
  const transferActive = state.paste?.status === "active";
  elements.commandInput.disabled = !world || !connection.ready || transferActive;
  elements.sendButton.disabled = !world || !connection.ready || transferActive;
  elements.historyButton.disabled = !world;
}

function renderWorldActivity() {
  const indicators = state.worlds.map((world) => {
    const unread = state.unread.get(world.world) || 0;
    const indicator = document.createElement("span");
    indicator.className = `world-activity-dot${world.world === state.selectedWorld ? " active" : ""}${unread ? " unread" : ""}`;
    indicator.title = unread
      ? `${world.world}: ${unread} unread message${unread === 1 ? "" : "s"}`
      : world.world;
    return indicator;
  });
  elements.worldActivity.replaceChildren(...indicators);
  const activeWorlds = state.worlds.filter(
    (world) => world.world !== state.selectedWorld && (state.unread.get(world.world) || 0) > 0,
  );
  const unread = activeWorlds.reduce(
    (total, world) => total + (state.unread.get(world.world) || 0),
    0,
  );
  elements.worldActivity.setAttribute(
    "aria-label",
    unread
      ? `${unread} unread message${unread === 1 ? "" : "s"} in ${activeWorlds.length} other world${activeWorlds.length === 1 ? "" : "s"}`
      : "No unread activity in other worlds",
  );
}

function restoreHistoryCommand(text) {
  if (!state.selectedWorld) return;
  elements.commandInput.value = text;
  state.drafts.set(state.selectedWorld, text.slice(0, MAX_STORED_COMMAND_CHARACTERS));
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
  if (!world || elements.historyDialog.open || historyDialogPending) return;
  historyDialogPending = true;
  elements.commandInput.blur();
  elements.historyDialogWorld.textContent = world.world;
  const history = state.commandHistory.get(world.world) || [];
  const buttons = [...history].reverse().map((text) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.addEventListener("click", () => restoreHistoryCommand(text));
    return button;
  });
  elements.historyList.replaceChildren(...buttons);
  elements.historyEmpty.hidden = buttons.length > 0;
  window.requestAnimationFrame(() => {
    historyDialogPending = false;
    if (!elements.historyDialog.open) elements.historyDialog.showModal();
    scheduleViewportUpdate();
  });
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
  renderWorldActivity();
}

function selectWorld(worldName, { focusInput = true } = {}) {
  cancelScreenClearAnimation();
  if (state.selectedWorld) {
    state.drafts.set(
      state.selectedWorld,
      elements.commandInput.value.slice(0, MAX_STORED_COMMAND_CHARACTERS),
    );
    readingState().scrollTop = elements.transcript.scrollTop;
  }
  state.selectedWorld = worldName;
  safeStore("tfr.selectedWorld", worldName);
  state.unread.set(worldName, 0);
  state.historyIndex = null;
  elements.commandInput.value = state.drafts.get(worldName) || "";
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

function applyTextStyle(element, style) {
  const sheet = [...document.styleSheets].find((candidate) => candidate.href?.endsWith("/styles.css"));
  if (sheet) {
    try {
      installTextRunColors(style, sheet, installedTextRunColors);
    } catch {
      // Safe text and static emphasis remain available if CSSOM mutation is unavailable.
    }
  }
  element.classList.add(...textRunClassNames(style));
}

function appendEventText(
  container,
  event,
  { animate = true, animationsEnabled: animationOverride = null } = {},
) {
  const runs = validatedTextRuns(event);
  const animationsEnabled = animationOverride ?? motionAllowsAnimation(
      elements.motionPreference.value,
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    );
  let presentationWrappers = [];
  let activePresentations = [];
  for (const run of splitPresentationRuns(event, runs, animationsEnabled && animate, Date.now())) {
    const presentations = run.presentations || (run.presentation ? [run.presentation] : []);
    let shared = 0;
    while (
      shared < presentations.length &&
      activePresentations[shared]?.program === presentations[shared].program
    ) shared += 1;
    activePresentations = activePresentations.slice(0, shared);
    presentationWrappers = presentationWrappers.slice(0, shared);
    let parent = presentationWrappers.at(-1) || container;
    for (const presentation of presentations.slice(shared)) {
        const presentationWrapper = document.createElement("span");
        presentationWrapper.className = "presentation-target";
        if (presentation.characterReveal) {
          presentationWrapper.setAttribute(
            "aria-label",
            Array.from(event.text || "")
              .slice(presentation.program.start, presentation.program.end)
              .join(""),
          );
        }
        applyTextStyle(presentationWrapper, presentation.style);
        if (presentation.style.foreground) {
          presentationWrapper.style.color = presentation.style.foreground;
        }
        if (presentation.style.bold) presentationWrapper.style.fontWeight = "700";
        if (presentation.style.underline) presentationWrapper.style.textDecoration = "underline";
        if (presentation.style.reverse) {
          presentationWrapper.classList.add("presentation-reverse");
          presentationWrapper.style.setProperty(
            "--presentation-reverse-background",
            presentation.style.foreground || "var(--text)",
          );
        }
        parent.append(presentationWrapper);
        if (animate) {
          const animation = animatePresentationElement(
            presentationWrapper,
            presentation,
            event.timestamp,
          );
          if (animation) {
            presentationWrapper.classList.add("presentation-animated");
            const animatedWrapper = presentationWrapper;
            activePresentationAnimations.add(animation);
            const release = () => {
              animatedWrapper.classList.remove("presentation-animated");
              activePresentationAnimations.delete(animation);
            };
            animation.addEventListener("finish", release);
            animation.addEventListener("cancel", release);
          }
        }
        activePresentations.push(presentation);
        presentationWrappers.push(presentationWrapper);
        parent = presentationWrapper;
    }
    const styled = document.createElement("span");
    applyTextStyle(styled, run.style);
    const characterPresentations = presentations.filter((presentation) =>
      presentation.characterIndex !== null && (
        presentation.characterSweep || presentation.characterHueWave ||
        presentation.characterPaletteFront || presentation.characterStyleSweep ||
        presentation.characterSparkle || presentation.characterReveal
      )
    );
    if (characterPresentations.length) {
      styled.classList.add("presentation-character");
      styled.dataset.caseable = /^[A-Za-z]$/.test(run.text) ? "true" : "false";
      styled.dataset.originalText = run.text;
      const baseColor = characterPresentations.findLast(
        (presentation) => presentation.style.foreground,
      )?.style.foreground;
      if (baseColor) styled.style.color = baseColor;
    }
    if (
      presentations.some((presentation) => presentation.style.foreground) &&
      !characterPresentations.length &&
      !(run.role === "speaker" && event.spoof_status === "spoofed")
    ) {
      styled.classList.add("presentation-content");
    }
    if (run.role === "speaker" && event.spoof_status === "spoofed") {
      styled.classList.add("spoofed-speaker");
    }
    appendTextWithLinks(styled, run.text);
    parent.append(styled);
  }
}

function eventNode(event, { animate = true } = {}) {
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
  const displayedEvent = event.connection_state
    ? { ...event, text: connectionNotice(event), text_runs: undefined, presentation: undefined }
    : event;
  appendEventText(text, displayedEvent, { animate });
  body.append(text);

  item.append(body);
  if (animate) showCombo(event, item);
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
  elements.eventSheetBody.scrollTop = 0;
  scheduleViewportUpdate();
}

function cancelScreenClearAnimation() {
  const animation = activeScreenClearAnimation;
  activeScreenClearAnimation = null;
  animation?.cancel();
}

function screenClearEvents(world = state.selectedWorld) {
  const events = state.events.get(world) || [];
  return eventsAfterClearCutoff(events, state.screenClearCutoffs.get(world));
}

function showScreenRestoreHint() {
  try {
    if (sessionStorage.getItem("tfr.screenClearRestoreHintShown")) return;
    sessionStorage.setItem("tfr.screenClearRestoreHintShown", "true");
  } catch {
    // The one-time hint can still be shown when browser storage is unavailable.
  }
  const hint = document.createElement("div");
  hint.className = "screen-clear-hint";
  hint.textContent = "Swipe down to restore cleared output";
  elements.console.append(hint);
  window.setTimeout(() => hint.remove(), 3500);
}

function clearSelectedScreen() {
  const world = state.selectedWorld;
  if (!world || !readingState(world).atLive) return;
  const visibleEvents = screenClearEvents(world);
  const cutoff = visibleEvents.at(-1);
  if (!cutoff) return;
  const cells = captureTranscriptCells(elements.eventList, elements.transcript);
  const animationsEnabled = motionAllowsAnimation(
    elements.motionPreference.value,
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  const effect = selectScreenClearEffect(
    state.screenClearConfig,
    state.lastScreenClearEffects.get(world),
  );
  if (effect) state.lastScreenClearEffects.set(world, effect.name);
  state.screenClearCutoffs.set(world, cutoff.id);
  cancelScreenClearAnimation();
  renderTranscript({ scrollToLive: true });
  showScreenRestoreHint();
  if (!animationsEnabled || !cells.length || !effect) return;
  const animation = animateScreenClear({
    canvas: elements.screenClearOverlay,
    transcript: elements.transcript,
    cells,
    effect,
    seed: ++screenClearSeed,
    onFinish: () => {
      if (activeScreenClearAnimation === animation) activeScreenClearAnimation = null;
    },
  });
  activeScreenClearAnimation = animation;
}

function restoreSelectedScreen() {
  const world = state.selectedWorld;
  if (!world || !state.screenClearCutoffs.has(world)) return false;
  state.screenClearCutoffs.delete(world);
  cancelScreenClearAnimation();
  renderTranscript({ scrollToLive: true });
  if (
    motionAllowsAnimation(
      elements.motionPreference.value,
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    ) && typeof elements.eventList.animate === "function"
  ) {
    elements.eventList.animate(
      [
        { transform: "translateY(-1.5rem)", opacity: 0 },
        { transform: "translateY(0)", opacity: 1 },
      ],
      { duration: 180, easing: "ease-out" },
    );
  }
  return true;
}

function updateScreenClearHandle() {
  const cleared = state.screenClearCutoffs.has(state.selectedWorld);
  const label = cleared ? "Restore cleared output" : "Pull up to clear screen";
  elements.screenClearHandle.setAttribute("aria-label", label);
  elements.screenClearHandle.title = label;
  elements.screenClearHandle.disabled = !cleared && (
    !readingState().atLive || !screenClearEvents().length
  );
}

function renderTranscript({ scrollToLive = false, smoothScroll = false, restorePosition = false } = {}) {
  const renderStarted = performance.now();
  cancelPresentationAnimations();
  if (liveRenderFrame !== null) window.cancelAnimationFrame(liveRenderFrame);
  liveRenderFrame = null;
  pendingLiveEvents = [];
  const events = screenClearEvents();
  const visible = events.slice(-MAX_RENDERED_EVENTS);
  const reading = readingState();
  const nodes = [];
  let animationBudget = MAX_ANIMATED_PRESENTATIONS;
  for (const event of visible.toReversed()) {
    const cost = eventPresentationAnimationCost(event);
    const node = eventNode(event, { animate: animationBudget >= cost });
    animationBudget -= presentationAnimationCost(node);
    nodes.unshift(node);
  }
  elements.eventList.replaceChildren(...nodes);
  elements.emptyState.hidden = visible.length > 0 || state.screenClearCutoffs.has(state.selectedWorld);
  updateScreenClearHandle();
  const renderDuration = performance.now() - renderStarted;
  if (renderDuration >= SLOW_TRANSCRIPT_RENDER_MS) {
    reportResponsiveness(
      "transcript_render_slow",
      `Transcript render took ${Math.round(renderDuration)} ms for ${visible.length} events`,
      restorePosition ? "restore" : scrollToLive ? "return-live" : "render",
    );
  }
  const presentationCharacters = elements.eventList.querySelectorAll(
    ".presentation-character",
  ).length;
  const transcriptNodes = elements.eventList.getElementsByTagName("*").length;
  if (
    presentationCharacters > PRESENTATION_CHARACTER_PRESSURE ||
    transcriptNodes > TRANSCRIPT_NODE_PRESSURE
  ) {
    reportResponsiveness(
      "transcript_node_pressure",
      `Transcript contains ${transcriptNodes} nodes and ${presentationCharacters} presentation characters`,
      restorePosition ? "restore" : "render",
    );
  }
  if (restorePosition) {
    restoreTranscriptPosition({
      transcript: elements.transcript,
      reading,
      schedule: requestAnimationFrame,
      onRestore: updateReturnLive,
    });
  } else if (scrollToLive || reading.atLive) {
    requestAnimationFrame(() => {
      elements.transcript.scrollTo({
        top: elements.transcript.scrollHeight,
        behavior:
          scrollToLive &&
          smoothScroll &&
          motionAllowsAnimation(
            elements.motionPreference.value,
            window.matchMedia("(prefers-reduced-motion: reduce)").matches,
          )
            ? "smooth"
            : "auto",
      });
      reading.atLive = true;
      reading.unseenLive = 0;
      reading.scrollTop = elements.transcript.scrollTop;
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
    cancelPresentationAnimations();
    const retained = visible.slice(-MAX_RENDERED_EVENTS);
    const nodes = [];
    let animationBudget = MAX_ANIMATED_PRESENTATIONS;
    for (const event of retained.toReversed()) {
      const cost = eventPresentationAnimationCost(event);
      const node = eventNode(event, { animate: animationBudget >= cost });
      animationBudget -= presentationAnimationCost(node);
      nodes.unshift(node);
    }
    elements.eventList.replaceChildren(...nodes);
  } else {
    const fragment = document.createDocumentFragment();
    let animatedCount = [...elements.eventList.children].reduce(
      (total, node) => total + presentationAnimationCost(node),
      0,
    );
    for (const event of visible) {
      const animate =
        animatedCount + eventPresentationAnimationCost(event) <= MAX_ANIMATED_PRESENTATIONS;
      const node = eventNode(event, { animate });
      fragment.append(node);
      animatedCount += presentationAnimationCost(node);
    }
    elements.eventList.append(fragment);
  }
  while (elements.eventList.children.length > MAX_RENDERED_EVENTS) {
    const removed = elements.eventList.firstElementChild;
    for (const target of removed?.querySelectorAll(".presentation-animated") || []) {
      for (const animation of target.getAnimations()) animation.cancel();
    }
    removed?.remove();
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
  let eventIds = state.eventIds.get(event.world);
  if (eventIds?.has(event.id)) return;
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
  if (connection.ready) recordLiveHistoryEvent(world, event);
  if (connection.ready) state.cursor = message.cursor;
  if (!isTranscriptEvent(event)) return;
  if (!events) {
    events = [];
    eventIds = new Set();
    state.events.set(event.world, events);
    state.eventIds.set(event.world, eventIds);
  }
  if (connection.ready && state.speakingStreaksEnabled) {
    event.combo = validatedComboCue(event.combo, event.text)
      || observeCombo(state.comboStreaks, event, Date.now(), world);
  } else {
    event.combo = null;
  }
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

  if (!connection.ready) return;
  const reading = readingState(event.world);
  if (event.combo && event.world !== state.selectedWorld) {
    event.combo.presented = true;
  } else if (event.combo && !reading.atLive) {
    showCombo(event, null);
  }
  if (event.world !== state.selectedWorld) {
    state.unread.set(event.world, (state.unread.get(event.world) || 0) + 1);
    if (!reading.atLive) reading.unseenLive += 1;
    renderWorlds();
    return;
  }
  if (evicted) updateHistoryNotice();
  if (connection.ready && !reading.atLive) {
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
  cancelScreenClearAnimation();
  state.events.clear();
  state.eventIds.clear();
  state.unread.clear();
  state.reading.clear();
  state.shownHistoryNotices.clear();
  state.comboStreaks.clear();
  state.screenClearCutoffs.clear();
  state.lastScreenClearEffects.clear();
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

function applyGatewayConfiguration(envelope) {
  if (
    !envelope ||
    !Number.isSafeInteger(envelope.revision) ||
    envelope.revision < 0 ||
    !envelope.settings ||
    typeof envelope.settings !== "object" ||
    Array.isArray(envelope.settings) ||
    !envelope.requirements ||
    typeof envelope.requirements !== "object" ||
    Array.isArray(envelope.requirements)
  ) {
    return;
  }
  const supported = new Set([
    "ui.speaking_streaks.enabled",
    "ui.spellcheck.enabled",
    "ui.show_nospoof_prefix",
    "updates.enabled",
    "updates.channel",
    "updates.manifest_url",
    "updates.check_interval_seconds",
    "updates.initial_delay_seconds",
    "updates.jitter_seconds",
    "updates.timeout_seconds",
  ]);
  const issues = Object.keys(envelope.settings)
    .filter((key) => !supported.has(key))
    .slice(0, 100)
    .map((key) => `unsupported setting: ${key}`);
  if (typeof envelope.settings["ui.speaking_streaks.enabled"] === "boolean") {
    state.speakingStreaksEnabled = envelope.settings["ui.speaking_streaks.enabled"];
    if (!state.speakingStreaksEnabled) {
      state.comboStreaks.clear();
      cancelComboVisuals();
    }
  }
  if (typeof envelope.settings["ui.spellcheck.enabled"] === "boolean") {
    elements.commandInput.spellcheck = envelope.settings["ui.spellcheck.enabled"];
  }
  if (connection.socket?.readyState === WebSocket.OPEN) {
    connection.socket.send(
      JSON.stringify({
        type: "configuration_applied",
        request_id: crypto.randomUUID(),
        revision: envelope.revision,
        status: issues.length ? "unsupported" : "applied",
        issues,
      }),
    );
  }
}

function handleMessage(message) {
  if (!message || message.protocol !== 1 || typeof message.type !== "string") return;
  if (message.type.startsWith("paste_") && handlePasteUpdate(message)) return;
  if (message.type === "hello") {
    connection.ready = false;
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
    state.effectDemos = validatedEffectDemos(message.effect_demos);
    state.screenClearConfig = validatedScreenClearConfig(message.screen_clear);
    state.speakingStreaksEnabled = message.speaking_streaks_enabled !== false;
    applyGatewayConfiguration(message.configuration);
    if (!state.speakingStreaksEnabled) {
      state.comboStreaks.clear();
      cancelComboVisuals();
    }
    for (const world of state.worlds) discardEventsBeforeSnapshotGap(world);
    if (!state.worlds.some((world) => world.world === state.selectedWorld)) {
      state.selectedWorld = state.worlds[0]?.world || null;
    }
    if (state.selectedWorld) {
      safeStore("tfr.selectedWorld", state.selectedWorld);
      elements.commandInput.value = state.drafts.get(state.selectedWorld) || "";
    }
    showHistoryNotice("");
    renderWorlds();
    updateWorldHeader();
    return;
  }
  if (message.type === "ui_settings") {
    state.speakingStreaksEnabled = message.speaking_streaks_enabled !== false;
    if (!state.speakingStreaksEnabled) {
      state.comboStreaks.clear();
      cancelComboVisuals();
    }
    return;
  }
  if (message.type === "configuration") {
    applyGatewayConfiguration(message);
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
    connection.markReady(connection.socket);
    state.cursor = message.cursor;
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
  if (message.type === "maintenance") {
    const text =
      typeof message.message === "string"
        ? message.message
        : "Gateway maintenance is starting";
    setConnection("Updating", "connecting");
    showToast(text, 8000);
    return;
  }
  if (message.type === "ack") {
    if (state.paste?.requestId === message.request_id && message.ok !== true) {
      state.paste.status = "rejected";
      state.paste.message = message.error || "The Gateway rejected this transfer.";
      updateWorldHeader();
      renderPasteDialog();
      return;
    }
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
        state.drafts.set(
          pending.world,
          pending.draft.slice(0, MAX_STORED_COMMAND_CHARACTERS),
        );
        if (pending.world === state.selectedWorld) elements.commandInput.value = pending.draft;
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
  const socket = connection.socket;
  if (!connection.ready || !socket || socket.readyState !== WebSocket.OPEN) return;
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
    if (connection.isCurrent(socket) && state.heartbeatRequestId === requestId) {
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
  connection.cancelReconnect();
  if (
    connection.socket &&
    [WebSocket.OPEN, WebSocket.CONNECTING].includes(connection.socket.readyState)
  ) {
    return;
  }
  setConnection("Connecting");
  updateWorldHeader();
  const socket = new WebSocket(socketUrl());
  connection.begin(socket);

  socket.addEventListener("message", (event) => {
    if (!connection.isCurrent(socket)) return;
    try {
      handleMessage(JSON.parse(event.data));
    } catch {
      showToast("Received an invalid Gateway message", 4200);
    }
  });
  socket.addEventListener("close", async (event) => {
    const closeToken = connection.close(socket);
    if (closeToken === null) return;
    reportDiagnostic(recordDiagnostic({
      severity: event.code === 1000 ? "info" : "warning",
      component: "connection",
      code: "websocket_closed",
      message: `WebSocket closed with code ${event.code}`,
      operation: "websocket",
    }));
    stopHeartbeat();
    interruptPasteTransfer();
    updateWorldHeader();
    if (state.pending.size > 0) {
      for (const pending of state.pending.values()) {
        state.drafts.set(
          pending.world,
          pending.draft.slice(0, MAX_STORED_COMMAND_CHARACTERS),
        );
        if (pending.world === state.selectedWorld) elements.commandInput.value = pending.draft;
      }
      state.pending.clear();
      showToast("Command status unknown. Check the transcript before resending.", 6000);
    }
    const session =
      event.code === 1008 || connection.reconnectAttempt >= 2
        ? await sessionState()
        : "paired";
    if (!connection.closeIsCurrent(closeToken)) return;
    if (session === "unpaired") {
      await clearLocalData();
      elements.console.hidden = true;
      elements.pairing.hidden = false;
      elements.pairingMessage.textContent =
        "This device session expired or was revoked. Create a new link and paste it below.";
      return;
    }
    setConnection(
      navigator.onLine
        ? session === "unreachable"
          ? "Gateway unavailable"
          : "Reconnecting"
        : "Offline",
      "error",
    );
    connection.scheduleReconnect();
  });
  socket.addEventListener("error", () => socket.close());
}

function scheduleReconnect() {
  connection.scheduleReconnect();
}

function sendCommand(event) {
  event.preventDefault();
  acknowledgeInteraction(elements.sendButton);
  const world = currentWorld();
  const draft = normalizeWorldCommand(elements.commandInput.value);
  const text = commandWithAutomaticSay(draft, automaticSayPreference());
  if (
    !world ||
    !connection.ready ||
    !connection.socket ||
    connection.socket.readyState !== WebSocket.OPEN
  ) {
    showToast("Gateway is not ready");
    return;
  }
  if (!text || isMultilineWorldCommand(text) || text.includes("\0")) {
    showToast("Commands must be a single non-empty line");
    return;
  }
  if ([...state.pending.values()].some((pending) => pending.world === world.world)) {
    showToast("Wait for the previous command acknowledgement");
    return;
  }
  const requestId = crypto.randomUUID();
  state.pending.set(requestId, { world: world.world, text, draft });
  try {
    connection.socket.send(
      JSON.stringify({ type: "command", request_id: requestId, world: world.world, text }),
    );
  } catch {
    state.pending.delete(requestId);
    showToast("Command was not sent");
    return;
  }

  const storedHistory = state.commandHistory.get(world.world);
  const history = Array.isArray(storedHistory) ? storedHistory : [];
  const storedText = text.slice(0, MAX_STORED_COMMAND_CHARACTERS);
  if (history.at(-1) !== storedText) history.push(storedText);
  state.commandHistory.set(world.world, history.slice(-MAX_COMMAND_HISTORY));
  state.drafts.set(world.world, "");
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
  const history = state.commandHistory.get(world.world) || [];
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
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), CONNECTION_TIMEOUT_MS);
  try {
    const response = await fetch("/api/session", {
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
    if (!response.ok) return "unreachable";
    return (await response.json()).paired === true ? "paired" : "unpaired";
  } catch {
    return "unreachable";
  } finally {
    window.clearTimeout(timeout);
  }
}

async function clearLocalData() {
  stopHeartbeat();
  connection.stop()?.close();
  resetGatewayState();
  state.worlds = [];
  state.gatewayId = null;
  state.selectedWorld = null;
  state.drafts = new Map();
  state.commandHistory = new Map();
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

async function clearInstalledApplicationData() {
  await clearLocalData();
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    // Continue with browser-managed storage cleanup when DOM storage is unavailable.
  }
  if ("indexedDB" in window && "databases" in indexedDB) {
    try {
      for (const database of await indexedDB.databases()) {
        if (database.name) indexedDB.deleteDatabase(database.name);
      }
    } catch {
      // TFR currently has no required IndexedDB state; cleanup remains best-effort.
    }
  }
  if ("serviceWorker" in navigator) {
    try {
      for (const registration of await navigator.serviceWorker.getRegistrations()) {
        await registration.unregister();
      }
    } catch {
      // iOS may retain browser-managed data even after all app-owned state is cleared.
    }
  }
}

async function revokeCurrentDevice() {
  const response = await fetch("/api/logout", {
    method: "POST",
    credentials: "same-origin",
  });
  if (!response.ok) throw new Error("Logout was rejected");
}

async function unpairDevice() {
  if (!window.confirm("Unpair this phone and clear its local TFR data?")) return;
  connection.intentionalClose = true;
  try {
    await revokeCurrentDevice();
    await clearLocalData();
    location.reload();
  } catch {
    connection.intentionalClose = false;
    showToast("Could not revoke this device. Reconnect or revoke it from the Gateway.", 6000);
  }
}


async function uninstallDevice() {
  if (!window.confirm(
    "Revoke this phone, clear TFR browser data, and prepare the Home Screen app for removal?",
  )) return;
  connection.intentionalClose = true;
  try {
    await revokeCurrentDevice();
    await clearInstalledApplicationData();
    elements.settingsDialog.close();
    elements.uninstallDialog.showModal();
  } catch {
    connection.intentionalClose = false;
    showToast("Could not reset this device. Reconnect or revoke it from the Gateway.", 6000);
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
      if (shouldShowInstallOnboarding(
        pairingResult,
        document.documentElement.dataset.iosInstallMode,
      )) {
        elements.installOnboarding.hidden = false;
        elements.console.hidden = true;
        return;
      }
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
    setConnection(navigator.onLine ? "Gateway unavailable" : "Offline", "error");
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
  if (connection.socket?.readyState === WebSocket.OPEN && connection.ready) {
    sendHeartbeat();
    return;
  }
  const probeGeneration = connection.beginProbe();
  const session = await sessionState();
  if (!connection.probeIsCurrent(probeGeneration)) return;
  if (session === "unreachable") {
    setConnection(navigator.onLine ? "Gateway unavailable" : "Offline", "error");
    if (
      !connection.hasReconnectScheduled() &&
      connection.socket?.readyState !== WebSocket.CONNECTING
    ) {
      scheduleReconnect();
    }
    return;
  }
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
  connect();
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
elements.continueInBrowser.addEventListener("click", () => {
  elements.installOnboarding.hidden = true;
  elements.console.hidden = false;
  connect();
});
elements.uninstallDevice.addEventListener("click", uninstallDevice);
elements.composer.addEventListener("submit", sendCommand);
elements.sendButton.addEventListener(
  "touchstart",
  (event) => {
    event.preventDefault();
    elements.composer.requestSubmit(elements.sendButton);
  },
  { passive: false },
);
elements.commandInput.addEventListener("input", () => {
  if (!state.selectedWorld) return;
  state.drafts.set(
    state.selectedWorld,
    elements.commandInput.value.slice(0, MAX_STORED_COMMAND_CHARACTERS),
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
elements.commandInput.addEventListener("paste", (event) => {
  const text = event.clipboardData?.getData("text/plain") || "";
  if (!isMultilineWorldCommand(text)) return;
  event.preventDefault();
  openPastePreview(text);
});
elements.commandInput.addEventListener("focus", () => {
  preserveLiveDuringViewportChange = readingState().atLive;
  scheduleViewportUpdate();
});
elements.commandInput.addEventListener("blur", () => {
  preserveLiveDuringViewportChange = false;
  scheduleViewportUpdate();
});
elements.historyButton.addEventListener("click", openHistoryDialog);
elements.historyButton.addEventListener("pointerdown", (event) => {
  if (event.pointerType !== "touch") return;
  event.preventDefault();
  acknowledgeInteraction(elements.historyButton);
  openHistoryDialog();
});
elements.historyDialog.addEventListener("close", () => {
  historyDialogPending = false;
  elements.historyList.replaceChildren();
});
elements.textSmaller.addEventListener("click", () => applyTextSize(textSizePreference() - 1));
elements.textReset.addEventListener("click", () => applyTextSize(0));
elements.textLarger.addEventListener("click", () => applyTextSize(textSizePreference() + 1));
elements.motionPreference.addEventListener("change", () => {
  cancelComboVisuals();
  applyMotionPreference(elements.motionPreference.value);
  renderTranscript({ restorePosition: true });
});
elements.effectsLabButton.addEventListener("click", () => {
  renderEffectsLabCatalog();
  elements.settingsDialog.close();
  elements.effectsLab.showModal();
});
elements.diagnosticsButton.addEventListener("click", () => {
  const entries = diagnosticEntries();
  elements.diagnosticsOutput.textContent = entries.length
    ? JSON.stringify({
        gateway: elements.gatewayVersion.textContent,
        connection: elements.connectionState.textContent,
        service_worker: navigator.serviceWorker?.controller ? "controlling" : "not controlling",
        entries,
      }, null, 2)
    : "No local diagnostics.";
  elements.settingsDialog.close();
  elements.diagnosticsDialog.showModal();
});
elements.diagnosticsClose.addEventListener("click", () => elements.diagnosticsDialog.close());
elements.diagnosticsCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(elements.diagnosticsOutput.textContent);
    showToast("Diagnostics copied");
  } catch {
    showToast("Could not copy diagnostics");
  }
});
elements.diagnosticsClear.addEventListener("click", () => {
  clearDiagnostics();
  elements.diagnosticsOutput.textContent = "No local diagnostics.";
});
elements.effectsLabClose.addEventListener("click", () => {
  cancelComboVisuals();
  elements.effectsLabForceMotion.checked = false;
  elements.effectsLab.close();
});
elements.effectsLabCommand.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = elements.effectsLabCommandInput.value.trim();
  const command = parseEffectsLabCommand(value);
  if (command?.action === "streak") {
    if (command.target === "all") {
      let count = 3;
      const next = () => {
        if (!elements.effectsLab.open || count > 8) return;
        previewLabCombo(count);
        count += 1;
        scheduleCombo(next, 4000);
      };
      next();
    } else {
      previewLabCombo(Number(command.target));
    }
  } else if (command?.action === "speaker") {
    const demo = state.effectDemos.find((item) => item.label.toLocaleLowerCase() === command.target.toLocaleLowerCase());
    if (demo) previewLabSpeaker(demo);
  }
  elements.effectsLabCommandInput.value = "";
});
elements.lineWrap.addEventListener("change", () => {
  applyLineWrap(elements.lineWrap.checked);
});
elements.spellingSuggestions.addEventListener("change", () => {
  applySpellingSuggestions(elements.spellingSuggestions.checked);
});
elements.automaticSay.addEventListener("change", () => {
  applyAutomaticSay(elements.automaticSay.checked);
});
elements.pasteConfirm.addEventListener("click", startPasteTransfer);
elements.pasteStop.addEventListener("click", stopPasteTransfer);
elements.pasteCancel.addEventListener("click", () => {
  if (!state.paste || ["active", "stopping"].includes(state.paste.status)) return;
  elements.pasteDialog.close();
});
elements.pasteDialog.addEventListener("cancel", (event) => {
  if (state.paste && ["active", "stopping"].includes(state.paste.status)) {
    event.preventDefault();
    stopPasteTransfer();
  }
});
elements.pasteDialog.addEventListener("close", () => {
  if (!state.paste || !["active", "stopping"].includes(state.paste.status)) state.paste = null;
  updateWorldHeader();
});
elements.applyUpdate.addEventListener("click", () => {
  const worker = state.serviceWorkerRegistration?.waiting;
  if (!worker) return;
  state.reloadingForUpdate = true;
  elements.applyUpdate.disabled = true;
  worker.postMessage({ type: "SKIP_WAITING" });
});
elements.returnLive.addEventListener("click", () => {
  preserveLiveDuringViewportChange = document.activeElement === elements.commandInput;
  renderTranscript({ scrollToLive: true, smoothScroll: true });
});
elements.screenClearHandle.addEventListener("pointerdown", (event) => {
  if (!readingState().atLive || !screenClearEvents().length || event.button !== 0) return;
  screenClearPull = { pointerId: event.pointerId, startY: event.clientY, armed: false };
  elements.screenClearHandle.setPointerCapture(event.pointerId);
  elements.screenClearHandle.dataset.pulling = "true";
  elements.screenClearHandle.style.setProperty("--screen-clear-pull", "0px");
});
elements.screenClearHandle.addEventListener("pointermove", (event) => {
  if (!screenClearPull || event.pointerId !== screenClearPull.pointerId) return;
  const distance = Math.max(0, screenClearPull.startY - event.clientY);
  screenClearPull.armed = distance >= SCREEN_CLEAR_PULL_DISTANCE;
  elements.screenClearHandle.toggleAttribute("data-armed", screenClearPull.armed);
  elements.screenClearHandle.style.setProperty(
    "--screen-clear-pull",
    `${-Math.min(distance, SCREEN_CLEAR_PULL_DISTANCE + 24)}px`,
  );
});
function finishScreenClearPull(event, cancelled = false) {
  if (!screenClearPull || event.pointerId !== screenClearPull.pointerId) return;
  const armed = screenClearPull.armed && !cancelled;
  screenClearPull = null;
  elements.screenClearHandle.removeAttribute("data-pulling");
  elements.screenClearHandle.removeAttribute("data-armed");
  elements.screenClearHandle.style.removeProperty("--screen-clear-pull");
  if (armed) clearSelectedScreen();
}
elements.screenClearHandle.addEventListener("pointerup", (event) => finishScreenClearPull(event));
elements.screenClearHandle.addEventListener("pointercancel", (event) => finishScreenClearPull(event, true));
elements.screenClearHandle.addEventListener("click", (event) => {
  if (event.detail !== 0) return;
  if (!restoreSelectedScreen()) clearSelectedScreen();
});
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
    preserveLiveDuringViewportChange = false;
    if (document.activeElement?.classList?.contains("event")) document.activeElement.blur();
    if (event.touches.length !== 1 || event.target.closest?.("a")) {
      transcriptTouch = null;
      return;
    }
    const touch = event.touches[0];
    transcriptTouch = {
      x: touch.clientX,
      y: touch.clientY,
      time: performance.now(),
      atTop: elements.transcript.scrollTop <= 1,
      canRestore: state.screenClearCutoffs.has(state.selectedWorld),
    };
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
    const end = {
      x: touch.clientX,
      y: touch.clientY,
      time: performance.now(),
    };
    if (start.canRestore && isRestoreSwipe(start, end, start.atTop)) {
      suppressTranscriptClickUntil = performance.now() + 500;
      restoreSelectedScreen();
      return;
    }
    const direction = swipeDirection(start, end);
    const moved = Math.hypot(touch.clientX - start.x, touch.clientY - start.y) > 10;
    if (moved) suppressTranscriptClickUntil = performance.now() + 500;
    if (direction && lineWrapPreference()) {
      switchWorld(direction);
    }
  },
  { passive: true },
);
document.addEventListener("keydown", () => setInputModality("keyboard"), { capture: true });
document.addEventListener("keydown", (event) => {
  if (!event.ctrlKey || event.altKey || event.metaKey || event.key.toLocaleLowerCase() !== "l") return;
  event.preventDefault();
  clearSelectedScreen();
});
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
  updateScreenClearHandle();
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

export const startup = start();
