function decimalInteger(value) {
  return typeof value === "string" && /^\d+$/.test(value) ? BigInt(value) : null;
}

function currentGenerationEvents(world, events) {
  return events.filter((event) => event.connection_generation === world?.connection_generation);
}

export function generationIsNewer(candidate, current) {
  const candidateValue = decimalInteger(candidate);
  const currentValue = decimalInteger(current);
  return candidateValue !== null && (currentValue === null || candidateValue > currentValue);
}

export function recordLiveHistoryEvent(world, event) {
  const history = world?.history;
  if (!history || history.connection_generation !== event?.connection_generation) return false;
  const available = decimalInteger(history.available_count);
  if (available === null) return false;
  history.available_count = String(available + 1n);
  return true;
}

export function historyNoticeMessage(world, events) {
  const history = world?.history;
  if (!history || history.connection_generation !== world.connection_generation) return null;
  const available = decimalInteger(history.available_count);
  if (available === null) return null;
  const loaded = currentGenerationEvents(world, events).length;
  return available > BigInt(loaded) ? `Only the last ${loaded} messages are shown.` : null;
}

export function historySnapshotHasGap(world, events) {
  return (
    world?.history?.connection_generation === world.connection_generation &&
    world.history.snapshot_gap === true &&
    currentGenerationEvents(world, events).length > 0
  );
}

export function historyNoticeDecision(world, events, shownGeneration, activeKey) {
  const message = historyNoticeMessage(world, events);
  if (!message) return { action: "hide", key: null };
  const key = `${world.world}:${world.connection_generation}`;
  if (shownGeneration === world.connection_generation) {
    return activeKey === key ? { action: "keep", key } : { action: "hide", key: null };
  }
  return {
    action: "show",
    key,
    generation: world.connection_generation,
    message,
  };
}
