const MIN_DISTANCE = 64;
const MAX_DURATION_MS = 700;
const HORIZONTAL_DOMINANCE = 1.35;

export function swipeDirection(start, end) {
  if (!start || !end || end.time - start.time > MAX_DURATION_MS) return 0;
  const horizontal = end.x - start.x;
  const vertical = end.y - start.y;
  if (
    Math.abs(horizontal) < MIN_DISTANCE ||
    Math.abs(horizontal) < Math.abs(vertical) * HORIZONTAL_DOMINANCE
  ) {
    return 0;
  }
  return horizontal < 0 ? 1 : -1;
}
