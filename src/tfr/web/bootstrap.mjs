const SHELL_CACHE_PREFIX = "tfr-shell-";

export function iphoneInstallMode({ userAgent, maxTouchPoints, standalone, displayModeStandalone }) {
  if (standalone || displayModeStandalone) return "installed";
  const ios = /iPhone|iPad|iPod/i.test(userAgent)
    || (/Macintosh/i.test(userAgent) && maxTouchPoints > 1);
  return ios ? "browser" : "other";
}

export async function repairAppShell({ serviceWorker, cacheStorage }) {
  const registrations = serviceWorker ? await serviceWorker.getRegistrations() : [];
  await Promise.all(registrations.map((registration) => registration.unregister()));
  if (cacheStorage) {
    const keys = await cacheStorage.keys();
    await Promise.all(
      keys
        .filter((key) => key.startsWith(SHELL_CACHE_PREFIX))
        .map((key) => cacheStorage.delete(key)),
    );
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
  const mode = iphoneInstallMode({
    userAgent: navigator.userAgent,
    maxTouchPoints: navigator.maxTouchPoints || 0,
    standalone: navigator.standalone === true,
    displayModeStandalone: window.matchMedia("(display-mode: standalone)").matches,
  });
  if (installHelp) installHelp.hidden = mode !== "browser";
  let installHintDismissed = false;
  try {
    installHintDismissed = localStorage.getItem("tfr.installHintDismissed") === "yes";
  } catch {
    // Installation guidance remains available when storage is unavailable.
  }
  if (installNotice) installNotice.hidden = mode !== "browser" || installHintDismissed;
  document.querySelector("#ios-install-dismiss")?.addEventListener("click", () => {
    if (installNotice) installNotice.hidden = true;
    try {
      localStorage.setItem("tfr.installHintDismissed", "yes");
    } catch {
      // Dismissal remains effective for the current page.
    }
  });

  document.querySelector("#startup-reload")?.addEventListener("click", () => location.reload());
  document.querySelector("#startup-repair")?.addEventListener("click", async () => {
    showStartupFailure("Repairing cached app files…");
    try {
      await repairAppShell({
        serviceWorker: navigator.serviceWorker,
        cacheStorage: window.caches,
      });
    } finally {
      location.reload();
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
    showStartupFailure("TFR could not load its app files. Repair the cached app and try again.");
  }
}

if (typeof document !== "undefined") bootstrap();
