/**
 * A scale for a value axis: the largest tick, which is at or above `max`, and the ticks from zero
 * to it in equal steps of 1, 2, 2.5 or 5 times a power of ten. A chart drawn to `top` shows its
 * tallest value inside the plot with a tidy number on the axis.
 */
export interface NiceScale {
  readonly top: number;
  readonly ticks: readonly number[];
}

const STEPS = [1, 2, 2.5, 5, 10] as const;

/** At most `count + 1` ticks (zero included) up to a tidy `top` that covers `max`. */
export function niceScale(max: number, count: number): NiceScale {
  const target = Math.max(1, Math.floor(count));
  if (!Number.isFinite(max) || max <= 0) return {top: 1, ticks: [0, 1]};
  const raw = max / target;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = (STEPS.find(each => each * power >= raw * (1 - 1e-12)) ?? 10) * power;
  const steps = Math.max(1, Math.ceil(max / step - 1e-12));
  return {top: steps * step, ticks: Array.from({length: steps + 1}, (_, at) => at * step)};
}
