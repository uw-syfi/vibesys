/**
 * The stretch of a long run the performance charts show, and what one chart draws of it. The run's
 * charts share one time axis. A run of at most `LONG_RUN_MS` is always shown whole; a longer one
 * offers the last hour, the last six, the last day, and all of it, and a window dragged on the
 * overview strip. A choice of "the last hour" is kept as the choice, so it follows a live run's end.
 * Pure arithmetic over served points: the best-so-far line is the host's marks, and a window only
 * cuts it (carrying in the best standing at its start).
 */
import type {MeasuredPointView, ObjectiveReport} from './objectives.js';

/** A stretch of time. */
export interface Span {
  readonly fromMs: number;
  readonly toMs: number;
}

const HOUR = 3_600_000;
/** A run longer than this offers a range control and an overview strip. */
export const LONG_RUN_MS = 6 * HOUR;

/** The ranges offered: the last hour, six hours, a day, or the whole run. */
export type RangeChoice = 'hour' | 'six-hours' | 'day' | 'all';

const LENGTH: Readonly<Record<Exclude<RangeChoice, 'all'>, number>> = {
  hour: HOUR,
  'six-hours': 6 * HOUR,
  day: 24 * HOUR,
};

/** What the reader chose to see: the whole run, its end (the last of something), or a dragged stretch. */
export type RangeView =
  | {readonly kind: 'all'}
  | {readonly kind: 'last'; readonly choice: Exclude<RangeChoice, 'all'>}
  | {readonly kind: 'between'; readonly fromMs: number; readonly toMs: number};

export const WHOLE_RUN: RangeView = {kind: 'all'};

/** Whether a run over `span` offers ranges at all. */
export const offersRanges = (span: Span): boolean => span.toMs - span.fromMs > LONG_RUN_MS;

/** The ranges a run over `span` offers, shortest first; none for a run that is not long. */
export function rangeChoices(span: Span): readonly RangeChoice[] {
  if (!offersRanges(span)) return [];
  const length = span.toMs - span.fromMs;
  return [
    ...(['hour', 'six-hours', 'day'] as const).filter(choice => LENGTH[choice] < length),
    'all',
  ];
}

/** The stretch `view` shows of a run over `span`, cut to it; the whole span when the run is not long. */
export function windowOf(view: RangeView, span: Span): Span {
  if (!offersRanges(span) || view.kind === 'all') return span;
  if (view.kind === 'last') {
    return {fromMs: Math.max(span.fromMs, span.toMs - LENGTH[view.choice]), toMs: span.toMs};
  }
  const fromMs = Math.min(Math.max(span.fromMs, view.fromMs), span.toMs);
  const toMs = Math.max(fromMs, Math.min(span.toMs, view.toMs));
  return toMs > fromMs ? {fromMs, toMs} : span;
}

/** The choice `view` is, or null for a stretch dragged on the strip, which is none of them. */
export const choiceOf = (view: RangeView): RangeChoice | null =>
  view.kind === 'all' ? 'all' : view.kind === 'last' ? view.choice : null;

/** What one chart draws of its objective in a window. */
export interface BandView {
  /** The measurements inside the window, oldest first. */
  readonly points: readonly MeasuredPointView[];
  /** The corners of the best-so-far line inside the window: the points that set a best. */
  readonly corners: readonly MeasuredPointView[];
  /**
   * The best-so-far line across the window: the best standing at its start (when there is one),
   * each corner, and the line carried on to the window's end. Empty with nothing measured yet.
   */
  readonly steps: readonly {readonly x: number; readonly y: number}[];
  /** The values the chart's value axis spans, a little wider than the data. */
  readonly domain: readonly [number, number];
  /** The standing bound lies near the data, so the axis spans it and the chart draws its line. */
  readonly boundDrawn: boolean;
}

/**
 * From when every measurement of the objective is in what the host served, or null when none is
 * missing. A host that cuts a long series keeps the points that set a best and then the newest of
 * the others, so the others are all there from the oldest one kept: before it, only the best-so-far
 * points remain, and the line summarises the rest.
 */
export function historyStart(objective: ObjectiveReport, truncated: boolean): number | null {
  if (!truncated) return null;
  const others = objective.points.filter(point => !point.bestSoFar);
  return others.length === 0 ? null : Math.min(...others.map(point => point.atMs));
}

/** The share of the value range left above and below the data, so no point sits on the frame. */
const PAD = 0.1;
/** A bound farther than this share of the data's magnitude outside it is not drawn: it would flatten the data. */
const BOUND_REACH = 0.35;

/** Whether the standing bound is near enough the data to be drawn on its chart: within a third of the data's own size outside it. */
export function boundIsDrawn(low: number, high: number, bound: number): boolean {
  return bound >= low - BOUND_REACH * Math.abs(low) && bound <= high + BOUND_REACH * Math.abs(high);
}

/** The first index whose point is at or after `at`. */
function firstAtOrAfter(points: readonly MeasuredPointView[], at: number): number {
  let low = 0;
  let high = points.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if ((points[mid]?.atMs ?? 0) < at) low = mid + 1;
    else high = mid;
  }
  return low;
}

/** What the chart of `objective` draws in `window`; the line is carried on to `window.toMs`. */
export function bandView(objective: ObjectiveReport, window: Span): BandView {
  const all = objective.points;
  const start = firstAtOrAfter(all, window.fromMs);
  const end = firstAtOrAfter(all, window.toMs + 1);
  const points = all.slice(start, end);
  const corners = points.filter(point => point.bestSoFar);
  // The best standing when the window opens: the newest corner before it.
  const before = all
    .slice(0, start)
    .reverse()
    .find(point => point.bestSoFar);
  const line = [...(before === undefined ? [] : [before]), ...corners];
  const last = line.at(-1);
  const steps = [
    ...line.map((point, at) => ({
      x: at === 0 && before !== undefined ? window.fromMs : point.atMs,
      y: point.value,
    })),
    ...(last === undefined ? [] : [{x: window.toMs, y: last.value}]),
  ];
  const values = [
    ...points.map(point => point.value),
    ...(before === undefined ? [] : [before.value]),
  ];
  const bound = objective.bound?.current.value;
  let low = values.length === 0 ? 0 : Math.min(...values);
  let high = values.length === 0 ? 1 : Math.max(...values);
  const boundDrawn = bound !== undefined && values.length > 0 && boundIsDrawn(low, high, bound);
  if (boundDrawn) {
    low = Math.min(low, bound);
    high = Math.max(high, bound);
  }
  const room = (high - low || Math.abs(high) || 1) * PAD;
  return {points, corners, steps, domain: [low - room, high + room], boundDrawn};
}
