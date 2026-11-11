export function runAtWindowLoad(documentObject, windowObject, callback) {
  if (documentObject.readyState === "complete") {
    callback();
    return "immediate";
  }
  windowObject.addEventListener("load", callback, { once: true });
  return "deferred";
}

export function workerDescriptor(worker, origin) {
  if (!worker) return null;
  let scriptPath = "unknown";
  try {
    const script = new URL(worker.scriptURL, origin);
    scriptPath = script.origin === origin ? script.pathname : "cross-origin";
  } catch {
    // Keep the bounded unknown marker.
  }
  return { state: worker.state || "unknown", script_path: scriptPath };
}
