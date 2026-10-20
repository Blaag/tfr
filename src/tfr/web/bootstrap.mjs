export function iphoneInstallMode({ userAgent, maxTouchPoints, standalone, displayModeStandalone }) {
  if (standalone || displayModeStandalone) return "installed";
  const ios = /iPhone|iPad|iPod/i.test(userAgent)
    || (/Macintosh/i.test(userAgent) && maxTouchPoints > 1);
  return ios ? "browser" : "other";
}

export function iosBrowserName(userAgent) {
  if (/CriOS/i.test(userAgent)) return "Chrome";
  if (/EdgiOS/i.test(userAgent)) return "Edge";
  if (/FxiOS/i.test(userAgent)) return "Firefox";
  return /Safari/i.test(userAgent) ? "Safari" : "your browser";
}

export function iosInstallInstructions(browser) {
  const instructions = [
    `In ${browser}, open the Share menu.`,
    "Choose Add to Home Screen.",
    "Tap Add, then open TFR from its new Home Screen icon.",
  ];
  if (browser !== "Safari") {
    instructions.push("If Add to Home Screen is unavailable, open this page in Safari.");
  }
  return instructions;
}

function replaceInstructions(element, instructions) {
  if (!element) return;
  element.replaceChildren();
  for (const text of instructions) {
    const item = document.createElement("li");
    item.textContent = text;
    element.append(item);
  }
}

function showStartupFailure(message) {
  const status = document.querySelector("#startup-status");
  const actions = document.querySelector("#startup-actions");
  if (status) status.textContent = message;
  if (actions) actions.hidden = false;
}

async function bootstrap() {
  const startup = document.querySelector("#startup");
  const installHelp = document.querySelector("#ios-install-help");
  const installNotice = document.querySelector("#ios-install-notice");
  const browser = iosBrowserName(navigator.userAgent);
  const mode = iphoneInstallMode({
    userAgent: navigator.userAgent,
    maxTouchPoints: navigator.maxTouchPoints || 0,
    standalone: navigator.standalone === true,
    displayModeStandalone: window.matchMedia("(display-mode: standalone)").matches,
  });
  document.documentElement.dataset.iosInstallMode = mode;
  document.documentElement.dataset.iosBrowser = browser;
  if (installHelp) installHelp.hidden = mode !== "browser";
  const instructions = iosInstallInstructions(browser);
  replaceInstructions(document.querySelector("#ios-install-steps"), instructions);
  replaceInstructions(document.querySelector("#install-onboarding-steps"), instructions);
  const installNoticeText = document.querySelector("#ios-install-notice-text");
  if (installNoticeText) {
    installNoticeText.textContent =
      `Install TFR from ${browser}: open Share, then choose Add to Home Screen.`;
  }
  let installHintDismissed = false;
  try {
    installHintDismissed = localStorage.getItem("tfr.installHintDismissed") === "yes";
  } catch {
    // Installation guidance remains available when storage is unavailable.
  }
  const completingPairing = new URLSearchParams(location.search).get("pairing") === "complete";
  if (installNotice) {
    installNotice.hidden = mode !== "browser" || installHintDismissed || completingPairing;
  }
  document.querySelector("#ios-install-dismiss")?.addEventListener("click", () => {
    if (installNotice) installNotice.hidden = true;
    try {
      localStorage.setItem("tfr.installHintDismissed", "yes");
    } catch {
      // Dismissal remains effective for the current page.
    }
  });

  const slow = window.setTimeout(() => {
    showStartupFailure("TFR is taking longer than expected to start.");
  }, 8000);
  try {
    const application = await import("./app.mjs");
    await application.startup;
    window.clearTimeout(slow);
    if (startup) startup.hidden = true;
  } catch (error) {
    window.clearTimeout(slow);
    console.error("TFR startup failed", error);
    reportDiagnostic(recordDiagnostic({
      severity: "error",
      component: "startup",
      code: "application_import_failed",
      message: "The PWA application module failed to initialize",
      operation: "bootstrap",
    }));
    showStartupFailure("TFR could not load its app files. Repair the cached app and try again.");
  }
}

if (typeof document !== "undefined") bootstrap();
import { recordDiagnostic, reportDiagnostic } from "./diagnostics.mjs";
