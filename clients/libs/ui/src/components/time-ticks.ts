/**
 * The ticks of a time axis: moments at even, tidy intervals, equally spaced, inside the span. The step is
 * the finest of a ladder of tidy ones (1, 2, 5, 10, 15 or 30 seconds or minutes, 1, 2, 3, 6 or 12
 * hours, whole days) that gives at most `most` ticks, and every tick is a multiple of the step, so
 * a minute axis is labelled on whole minutes whatever moment the span starts at. Spans are in
 * milliseconds. Hour and day steps are counted from the epoch of a clock: UTC, or the clock whose
 * offset from UTC `zone` tells, so a day axis ticks at that clock's midnights.
 */
import {labelsFit} from './label-fit.js';

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
  ...[1, 2, 3, 7, 14, 30].map(each => each * DAY),
];

/** The offset of a clock from UTC, in milliseconds, at a moment: what `zone` tells. */
export type ZoneOffset = (at: number) => number;

const UTC: ZoneOffset = () => 0;

/** How many multiples of `step` of the clock fall in the span. */
function countAt(from: number, to: number, step: number, zone: ZoneOffset): number {
  return Math.max(
    0,
    Math.floor((to + zone(to)) / step) - Math.ceil((from + zone(from)) / step) + 1,
  );
}

/** The multiples of `step` of the clock inside the span, at most `take`, ascending. */
function gridTicks(
  from: number,
  to: number,
  step: number,
  zone: ZoneOffset,
  take: number,
): readonly number[] {
  const start = Math.ceil((from + zone(from)) / step);
  const offset = zone(from);
  const ticks: number[] = [];
  const length = Math.min(countAt(from, to, step, zone) + 1, take + 1);
  for (let at = 0; at < length; at += 1) {
    const local = (start + at) * step;
    const moment = local - zone(local - offset);
    if (moment >= from && moment <= to) ticks.push(moment);
  }
  return ticks.slice(0, take);
}

/** The moments, ascending, from `from` to `to` (both included when they fall on a step). At most `most` of them. */
export function timeTicks(
  from: number,
  to: number,
  most: number,
  zone: ZoneOffset = UTC,
): readonly number[] {
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from || most < 1) return [];
  const limit = Math.floor(most);
  for (const step of STEPS) {
    if (countAt(from, to, step, zone) <= limit) return gridTicks(from, to, step, zone, limit);
  }
  return gridTicks(from, to, STEPS[STEPS.length - 1] ?? DAY, zone, limit);
}

/** The most ticks an axis draws when its width is not known. */
const UNMEASURED = 5;
/** The most ticks an axis draws at any width. */
const MOST = 12;

/** A tick of an axis with its label. */
export interface LabelledTick {
  readonly at: number;
  readonly label: string;
}

/**
 * The ticks of a time axis `width` pixels wide (null when not measured), labelled by `label(at, span)`.
 * Each tick is drawn centred on its moment, so the spacing on screen is the spacing of the moments:
 * of the tidy steps, the one that gives the most ticks whose labels all lie inside the axis and
 * clear of each other (the finest on a tie). A moment too near an end for its label to fit is
 * no tick of the axis; nothing slides inward, which would leave the gaps uneven.
 */
export function fittedTimeTicks(
  from: number,
  to: number,
  width: number | null,
  label: (at: number, span: number) => string,
  zone: ZoneOffset = UTC,
): readonly LabelledTick[] {
  const span = to - from;
  const labelled = (moments: readonly number[]): LabelledTick[] =>
    moments.map(at => ({at, label: label(at, span)}));
  if (!Number.isFinite(span) || span <= 0) return [];
  if (width === null) return labelled(timeTicks(from, to, UNMEASURED, zone));
  let best: LabelledTick[] = [];
  for (const step of STEPS) {
    if (countAt(from, to, step, zone) > MOST) continue;
    const share = (tick: LabelledTick) => ({share: (tick.at - from) / span, text: tick.label});
    // Only the first and last ticks can lie too near an end to be centred inside the axis.
    const kept = labelled(gridTicks(from, to, step, zone, MOST)).filter(tick =>
      labelsFit([share(tick)], width),
    );
    if (kept.length > best.length && labelsFit(kept.map(share), width)) best = kept;
  }
  return best;
}
