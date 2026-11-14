export function applicationServerKeyBytes(value) {
  if (typeof value !== "string" || !value) throw new Error("Missing Web Push public key");
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4);
  const decoded = atob(padded);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

export function normalizePushPreferences(value, allowedWorlds = []) {
  const allowed = new Set(allowedWorlds);
  const worlds = Array.isArray(value?.worlds)
    ? [...new Set(value.worlds.filter((world) => typeof world === "string" && allowed.has(world)))]
    : [];
  return {
    any_activity: value?.any_activity === true,
    worlds,
    mentions: value?.mentions !== false,
    pages: value?.pages !== false,
    preview: value?.preview === "details" ? "details" : "private",
  };
}

export function pushSubscriptionJSON(subscription) {
  const value = subscription?.toJSON?.();
  if (
    !value || typeof value.endpoint !== "string" ||
    typeof value.keys?.p256dh !== "string" || typeof value.keys?.auth !== "string"
  ) {
    throw new Error("Browser returned an invalid Web Push subscription");
  }
  return { endpoint: value.endpoint, keys: { p256dh: value.keys.p256dh, auth: value.keys.auth } };
}

async function jsonRequest(fetchImpl, url, options = {}) {
  const response = await fetchImpl(url, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  if (!response.ok) throw new Error((await response.text()) || `Request failed (${response.status})`);
  return response.json();
}

export function pushIsSupported(environment = globalThis) {
  return Boolean(
    environment?.navigator &&
    "Notification" in environment &&
    "PushManager" in environment &&
    "serviceWorker" in environment.navigator
  );
}

export async function loadPushStatus(fetchImpl = fetch) {
  return jsonRequest(fetchImpl, "/api/push");
}

export async function enablePush({ registration, publicKey, preferences, fetchImpl = fetch }) {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Notification permission was not granted");
  if (!registration?.pushManager) throw new Error("Web Push is unavailable in this installation");
  let subscription = await registration.pushManager.getSubscription();
  let created = false;
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: applicationServerKeyBytes(publicKey),
    });
    created = true;
  }
  try {
    return await jsonRequest(fetchImpl, "/api/push", {
      method: "PUT",
      body: JSON.stringify({ subscription: pushSubscriptionJSON(subscription), preferences }),
    });
  } catch (error) {
    if (created) await subscription.unsubscribe().catch(() => {});
    throw error;
  }
}

export async function updatePush({ registration, preferences, fetchImpl = fetch }) {
  const subscription = await registration?.pushManager?.getSubscription();
  if (!subscription) throw new Error("This installation has no Web Push subscription");
  return jsonRequest(fetchImpl, "/api/push", {
    method: "PUT",
    body: JSON.stringify({ subscription: pushSubscriptionJSON(subscription), preferences }),
  });
}

export async function disablePush({ registration, fetchImpl = fetch }) {
  await jsonRequest(fetchImpl, "/api/push", { method: "DELETE" });
  const subscription = await registration?.pushManager?.getSubscription();
  if (subscription) await subscription.unsubscribe();
  return { subscribed: false };
}
