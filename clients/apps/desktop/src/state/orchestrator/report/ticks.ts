/**
 * Where the value axis of the report's charts puts its labels: a few even numbers across a value
 * range. Pure arithmetic on numbers; the words of a label are the screen's. The time axis is the
 * chart's own: it picks its ticks to the width it is drawn at, and writes each as fine as its step.
 */

const STEPS = [1, 2, 2.5, 5, 10] as const;

/** At most about `wanted` round values within `[low, high]`, increasing; none for an empty range. */
export function valueTicks(low: number, high: number, wanted: number): readonly number[] {
  if (!Number.isFinite(low) || !Number.isFinite(high) || !(high > low) || wanted < 1) return [];
  const raw = (high - low) / wanted;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = (STEPS.find(each => each * power >= raw * (1 - 1e-12)) ?? 10) * power;
  const first = Math.ceil(low / step);
  const last = Math.floor(high / step);
  const ticks: number[] = [];
  for (let at = first; at <= last; at += 1) {
    const tick = Number((at * step).toPrecision(12));
    // Rounding to 12 figures can make two distinct multiples of `step` the same value when `step`
    // is many orders of magnitude smaller than `low`; drop a tick that did not move, so "increasing"
    // holds for every tick this function returns.
    if (tick >= low && tick <= high && tick !== ticks[ticks.length - 1]) ticks.push(tick);
  }
  return ticks;
}
