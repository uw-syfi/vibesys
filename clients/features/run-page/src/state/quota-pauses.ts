/**
 * The time agent turns spent stopped on a provider capacity limit, placed on the run clock so the
 * screens can tell it from agent work. The backend states each stop when it ends
 * (`quota_resumed`, `quota_abandoned`: how long it held the turn, stamped with the host's wall
 * time); the run's own clock relates to those stamps by the offset the live fold keeps
 * (`Heard.offset`), which puts each stop's interval on it. A step that spanned a pause keeps its
 * start and end, but its bar splits around the pause and its duration leaves the pause out.
 *
 * Pure over the stops and the offset; every function takes the pauses it reads.
 */
import type {QuotaStop} from '@vibesys/run-state';
import {duration} from './span-reading.js';

/** One stretch of the run clock a turn was held on a capacity limit. */
export interface QuotaPauseSpan {
  /** Run-clock seconds. */
  readonly from: number;
  readonly to: number;
  readonly provider: string;
  readonly condition: QuotaStop['condition'];
}

export const NO_PAUSES: readonly QuotaPauseSpan[] = [];

/**
 * The stops as run-clock intervals. `offsetSeconds` is a stamp minus the run time it was stamped at
 * (null before the page has heard one: nothing can be placed then). A stop with a stamp that is not a
 * time is left out.
 */
export function quotaPauses(
  stops: readonly QuotaStop[],
  offsetSeconds: number | null,
): readonly QuotaPauseSpan[] {
  if (offsetSeconds === null || stops.length === 0) return NO_PAUSES;
  const pauses: QuotaPauseSpan[] = [];
  for (const stop of stops) {
    const ended = Date.parse(stop.endedAt) / 1000;
    if (!Number.isFinite(ended)) continue;
    const to = ended - offsetSeconds;
    pauses.push({
      from: to - stop.waitedSeconds,
      to,
      provider: stop.provider,
      condition: stop.condition,
    });
  }
  return pauses;
}

/** The pauses' union as sorted, disjoint intervals clipped to [start, end]. */
function clipped(
  pauses: readonly QuotaPauseSpan[],
  start: number,
  end: number,
): readonly (readonly [number, number])[] {
  const inside = pauses
    .map(pause => [Math.max(start, pause.from), Math.min(end, pause.to)] as const)
    .filter(([from, to]) => to > from)
    .sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [from, to] of inside) {
    const last = merged.at(-1);
    if (last !== undefined && from <= last[1]) last[1] = Math.max(last[1], to);
    else merged.push([from, to]);
  }
  return merged;
}

/** Seconds of [start, end] spent paused: the union of the pauses over it, never counted twice. */
export function pausedSeconds(
  pauses: readonly QuotaPauseSpan[],
  start: number,
  end: number,
): number {
  return clipped(pauses, start, end).reduce((total, [from, to]) => total + (to - from), 0);
}

/** Seconds of [start, end] an agent could have worked: the whole stretch less its paused time. */
export function workedSeconds(
  pauses: readonly QuotaPauseSpan[],
  start: number,
  end: number,
): number {
  return Math.max(0, end - start - pausedSeconds(pauses, start, end));
}

/** A piece of [start, end]: worked time, or a pause. */
export interface PausePiece {
  readonly start: number;
  readonly end: number;
  readonly paused: boolean;
}

/** [start, end] cut at the pauses, in order: the pieces tile it, the paused ones stand for the pauses. */
export function piecesAround(
  pauses: readonly QuotaPauseSpan[],
  start: number,
  end: number,
): readonly PausePiece[] {
  const pieces: PausePiece[] = [];
  let at = start;
  for (const [from, to] of clipped(pauses, start, end)) {
    if (from > at) pieces.push({start: at, end: from, paused: false});
    pieces.push({start: from, end: to, paused: true});
    at = to;
  }
  if (end > at || pieces.length === 0)
    pieces.push({start: at, end: Math.max(at, end), paused: false});
  return pieces;
}

/** What a paused piece is called on a screen: `Quota pause · 37m`. */
export function pauseWords(seconds: number): string {
  return `Quota pause · ${duration(seconds)}`;
}

/** The time a stretch took, in words, with the pauses it spanned left out. */
export function workedWords(pauses: readonly QuotaPauseSpan[], start: number, end: number): string {
  return duration(workedSeconds(pauses, start, end));
}
