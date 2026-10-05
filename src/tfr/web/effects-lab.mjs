const COMBOS = {
  3: ["> Speaking Spree! <", "#ffffff", "Speaking Spree"],
  4: ["> Rampage! <", "#1eff00", "Rampage"],
  5: ["> Dominating! <", "#0070dd", "Dominating"],
  6: ["> Unstoppable! <", "#a335ee", "Unstoppable"],
  7: ["> GODLIKE! <", "#ff8000", "Godlike"],
  8: ["> GODLIKE x2 <", "#ff8000", "Godlike x2"],
};

export function comboLabDefinition(count) {
  const value = COMBOS[count];
  return value ? { count, notice: value[0], color: value[1], label: value[2] } : null;
}

export function parseEffectsLabCommand(value) {
  const streak = /^\/teststreak(?:\s+(all|[3-8]))?$/i.exec(value.trim());
  if (streak) return { action: "streak", target: (streak[1] || "all").toLocaleLowerCase() };
  const speaker = /^\/testspeaker\s+(.+)$/i.exec(value.trim());
  if (speaker) return { action: "speaker", target: speaker[1].trim() };
  return null;
}

export function validatedEffectDemos(value) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 100).flatMap((demo) => {
    if (
      !demo ||
      typeof demo.id !== "string" || demo.id.length > 100 ||
      typeof demo.label !== "string" || demo.label.length > 100 ||
      !["speaker", "text", "border", "screen", "combo"].includes(demo.category) ||
      !Array.isArray(demo.samples) ||
      (demo.worlds !== undefined && !Array.isArray(demo.worlds))
    ) return [];
    const samples = demo.samples.slice(0, 10).filter((sample) =>
      sample && typeof sample.text === "string" && Array.from(sample.text).length <= 2048
    );
    const worlds = (demo.worlds || []).slice(0, 100).filter((world) =>
      typeof world === "string" && world.length > 0 && world.length <= 100
    );
    return samples.length ? [{ ...demo, samples, worlds }] : [];
  });
}
