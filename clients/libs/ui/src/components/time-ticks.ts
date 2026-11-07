/**
 * The ticks of a time axis: moments at even, tidy intervals, equally spaced, inside the span. The step is
 * the finest of a ladder of tidy ones (1, 2, 5, 10, 15 or 30 seconds or minutes, 1, 2, 3, 6 or 12
 * hours, whole days) that gives at most `most` ticks, and every tick is a multiple of the step, so
 * a minute axis is labelled on whole minutes whatever moment the span starts at. Spans are in
 * milliseconds; hour and day steps are counted from the epoch, not from a local midnight.
 */
import {placeLabels} from './label-fit.js';

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const STEPS: readonly number[] = [
  1,
  2,
  5,
  10,
  20,
  50,
  100,
  200,
  500,
  ...[1, 2, 5, 10, 15, 30].map(each => each * SECOND),
  ...[1, 2, 5, 10, 15, 30].map(each => each * MINUTE),
  ...[1, 2, 3, 6, 12].map(each => each * HOUR),
  ...[1, 2, 7, 14, 30].map(each => each * DAY),
];

/** The moments, ascending, from `from` to `to` (both included when they fall on a step). At most `most` of them. */
export function timeTicks(from: number, to: number, most: number): readonly number[] {
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from || most < 1) return [];
  const count = (step: number): number =>
    Math.max(0, Math.floor(to / step) - Math.ceil(from / step) + 1);
  const within = (step: number, take: number): readonly number[] =>
    Array.from(
      {length: Math.min(count(step), take)},
      (_, at) => (Math.ceil(from / step) + at) * step,
    );
  const limit = Math.floor(most);
  for (const step of STEPS) {
    if (count(step) <= limit) return within(step, limit);
  }
  return within(STEPS[STEPS.length - 1] ?? DAY, limit);
}

/** The most ticks an axis draws when its width is not known. */
const UNMEASURED = 5;
const MOST = 12;

/** A tick of an axis with its label. */
export interface LabelledTick {
  readonly at: number;
  readonly label: string;
}

/**
 * The ticks of a time axis `width` pixels wide (null when not measured), labelled by `label(at, span)`:
 * the most tidy ticks whose labels can all be read side by side. Labels are never dropped to make
 * room, which would leave the ones that stay unevenly spaced; fewer, tidier ticks are drawn instead.
 */
export function fittedTimeTicks(
  from: number,
  to: number,
  width: number | null,
  label: (at: number, span: number) => string,
): readonly LabelledTick[] {
  const span = to - from;
  const labelled = (most: number): LabelledTick[] =>
    timeTicks(from, to, most).map(at => ({at, label: label(at, span)}));
  if (width === null) return labelled(UNMEASURED);
  for (let most = MOST; most > 1; most -= 1) {
    const ticks = labelled(most);
    const placed = placeLabels(
      ticks.map(tick => ({share: (tick.at - from) / span, text: tick.label})),
      width,
    );
    if (placed.length === ticks.length) return ticks;
  }
  return labelled(1);
}
