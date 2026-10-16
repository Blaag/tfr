const SHELL_CACHE_PREFIX = "tfr-shell-";

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

async function repair() {
  const status = document.querySelector("#repair-status");
  const returnLink = document.querySelector("#repair-return");
  try {
    await repairAppShell({
      serviceWorker: navigator.serviceWorker,
      cacheStorage: window.caches,
    });
    location.replace(`/?repaired=${Date.now()}`);
  } catch (error) {
    console.error("TFR repair failed", error);
    if (status) status.textContent = "Automatic repair failed. Return to TFR and try again.";
    if (returnLink) returnLink.hidden = false;
  }
}

if (typeof document !== "undefined") repair();
