/**
 * Where the axes of the report's charts put their labels: a few even numbers across a value range,
 * and a few round instants across a time range. Pure arithmetic on numbers; the words of a label are
 * the screen's.
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
    // holds for every tick this function returns, as it does for `timeTicks`.
    if (tick >= low && tick <= high && tick !== ticks[ticks.length - 1]) ticks.push(tick);
  }
  return ticks;
}

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** The spacings a time axis picks from, from a second to a week. */
const SPANS: readonly number[] = [
  SECOND,
  5 * SECOND,
  15 * SECOND,
  MINUTE,
  5 * MINUTE,
  15 * MINUTE,
  30 * MINUTE,
  HOUR,
  3 * HOUR,
  6 * HOUR,
  12 * HOUR,
  DAY,
  2 * DAY,
  7 * DAY,
];

/**
 * Round instants (multiples of one of a few spacings, on the clock of a zone) within `[fromMs, toMs]`,
 * about `wanted` of them, increasing. `offsetMs(atMs)` is how far the zone's clock is ahead of UTC at
 * an instant, so a day's tick is at the zone's midnight. Empty for an empty or reversed range.
 */
export function timeTicks(
  fromMs: number,
  toMs: number,
  wanted: number,
  offsetMs: (atMs: number) => number,
): readonly number[] {
  if (!(toMs > fromMs) || wanted < 1) return [];
  const raw = (toMs - fromMs) / wanted;
  const span = SPANS.find(each => each >= raw) ?? SPANS[SPANS.length - 1] ?? DAY;
  // A tick is a multiple of the span on the zone's clock; the offset is read at the tick itself, so
  // a change of the zone's offset inside the range (daylight saving) moves no tick off its clock time.
  const atLocal = (local: number): number => local - offsetMs(local - offsetMs(local));
  const first = Math.ceil((fromMs + offsetMs(fromMs)) / span) - 1;
  const last = Math.floor((toMs + offsetMs(toMs)) / span) + 1;
  const ticks: number[] = [];
  for (let at = first; at <= last; at += 1) {
    const tick = atLocal(at * span);
    const before = ticks[ticks.length - 1] ?? Number.NEGATIVE_INFINITY;
    if (tick >= fromMs && tick <= toMs && tick > before) ticks.push(tick);
  }
  return ticks;
}
