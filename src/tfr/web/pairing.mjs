export function createPairingCoordinator(pair) {
  let code = null;
  let inFlight = null;
  let initializing = true;
  let resumePending = false;

  async function attempt() {
    if (!code) return "none";
    if (inFlight) return inFlight;
    const attemptedCode = code;
    const attemptPromise = pair(attemptedCode);
    inFlight = attemptPromise;
    try {
      const result = await attemptPromise;
      if (result !== "unreachable" && code === attemptedCode) code = null;
      return result;
    } finally {
      if (inFlight === attemptPromise) inFlight = null;
    }
  }

  return {
    get code() {
      return code;
    },
    setCode(value) {
      code = value;
    },
    finishInitialization() {
      initializing = false;
      const pending = resumePending;
      resumePending = false;
      return pending;
    },
    attempt,
    async resume() {
      if (initializing) {
        resumePending = true;
        return "initializing";
      }
      return attempt();
    },
  };
}

export async function pairingResponse(response) {
  let value;
  try {
    value = await response.json();
  } catch {
    return response.ok
      ? { result: "unreachable" }
      : { result: "rejected", message: "Pairing failed" };
  }
  if (response.status === 429 || response.status >= 500) return { result: "unreachable" };
  const object = value !== null && typeof value === "object" ? value : {};
  if (!response.ok) {
    return {
      result: "rejected",
      message: typeof object.error === "string" ? object.error : "Pairing failed",
    };
  }
  return object.paired === true ? { result: "paired" } : { result: "unreachable" };
}
