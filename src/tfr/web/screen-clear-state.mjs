const MAX_GATEWAYS = 8;
const MAX_WORLDS = 64;
const IDENTIFIER = /^[\p{L}\p{N}_.:@ -]{1,128}$/u;
const EFFECT = /^[a-z0-9_]{1,64}$/;

export function parseScreenClearState(raw) {
  try {
    const value = JSON.parse(raw);
    if (!value || value.version !== 1 || typeof value.gateways !== "object") return new Map();
    const gateways = new Map();
    for (const [gateway, worlds] of Object.entries(value.gateways).slice(0, MAX_GATEWAYS)) {
      if (!IDENTIFIER.test(gateway) || !worlds || typeof worlds !== "object") continue;
      const entries = Object.entries(worlds)
        .slice(0, MAX_WORLDS)
        .filter(([world, effect]) => IDENTIFIER.test(world) && typeof effect === "string" && EFFECT.test(effect));
      gateways.set(gateway, new Map(entries));
    }
    return gateways;
  } catch {
    return new Map();
  }
}

export function serializeScreenClearState(gateways) {
  const output = {};
  if (!(gateways instanceof Map)) return JSON.stringify({ version: 1, gateways: output });
  for (const [gateway, worlds] of [...gateways].slice(-MAX_GATEWAYS)) {
    if (!IDENTIFIER.test(String(gateway)) || !(worlds instanceof Map)) continue;
    const entries = [...worlds]
      .slice(-MAX_WORLDS)
      .filter(([world, effect]) => IDENTIFIER.test(String(world)) && EFFECT.test(String(effect)));
    output[gateway] = Object.fromEntries(entries);
  }
  return JSON.stringify({ version: 1, gateways: output });
}

export function effectsForGateway(gateways, gateway) {
  if (!(gateways instanceof Map) || !IDENTIFIER.test(String(gateway || ""))) return new Map();
  const existing = gateways.get(gateway);
  if (existing instanceof Map) return new Map(existing);
  return new Map();
}

export function recordScreenClearEffect(gateways, gateway, world, effect) {
  if (
    !(gateways instanceof Map)
    || !IDENTIFIER.test(String(gateway || ""))
    || !IDENTIFIER.test(String(world || ""))
    || !EFFECT.test(String(effect || ""))
  ) return false;
  const worlds = gateways.get(gateway) instanceof Map ? gateways.get(gateway) : new Map();
  worlds.delete(world);
  worlds.set(world, effect);
  while (worlds.size > MAX_WORLDS) worlds.delete(worlds.keys().next().value);
  gateways.delete(gateway);
  gateways.set(gateway, worlds);
  while (gateways.size > MAX_GATEWAYS) gateways.delete(gateways.keys().next().value);
  return true;
}
