export function motionAllowsAnimation(preference, systemReducedMotion) {
  if (preference === "full") return true;
  if (preference === "reduced") return false;
  return !systemReducedMotion;
}
