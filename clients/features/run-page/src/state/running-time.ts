/**
 * The run's running time: its run clock with the stretches it stood stopped taken out, so a run
 * resumed a day later reads as the hours it worked. A stopped stretch is the gap between the last
 * time one session recorded and the first of the next (`RunSession`); a run clock that already
 * excludes its downtime leaves no gap to take out, and a run with no sessions known is read as it
 * stands.
 *
 * Pure: a mapping from run-clock seconds to seconds of running time, the same for every chart and
 * for the time bar, so the time bar's position and the charts' cursor name one moment.
 */
import type {RunSession} from '@vibesys/run-state/skeleton';

/** A stopped stretch of the run clock. */
interface Gap {
  readonly from: number;
  readonly to: number;
}

/** The run's running time as a mapping from its run clock. */
export interface RunningTime {
  /** Seconds of running time from the run's start at run-clock time `clock`; 0 before the start. */
  at(clock: number): number;
  /** Where each resume sits, in seconds of running time from the start, oldest first. */
  readonly resumes: readonly number[];
}

/** The stretches between the sessions: where one begins after the latest time the ones before it reached. */
function gapsBetween(sessions: readonly RunSession[]): readonly Gap[] {
  const gaps: Gap[] = [];
  let reached = Number.NEGATIVE_INFINITY;
  for (const session of sessions) {
    if (reached !== Number.NEGATIVE_INFINITY && session.from > reached) {
      gaps.push({from: reached, to: session.from});
    }
    reached = Math.max(reached, session.to);
  }
  return gaps;
}

/** `start` is the run's start on its clock: running time is counted from it. */
export function runningTimeOf(sessions: readonly RunSession[], start: number): RunningTime {
  const gaps = gapsBetween(sessions);
  const at = (clock: number): number => {
    const stopped = gaps.reduce(
      (total, gap) => total + Math.min(Math.max(clock - gap.from, 0), gap.to - gap.from),
      0,
    );
    return Math.max(0, clock - start - stopped);
  };
  return {at, resumes: sessions.slice(1).map(session => at(session.from))};
}

/**
 * Where each of the `seconds` (running time at each position of a sequence, ascending) stands along
 * a track from 0 (the first) to 1 (the last). A sequence that never advanced, or a single position,
 * is spread evenly by position.
 */
export function placesAlong(seconds: readonly number[]): readonly number[] {
  const first = seconds[0];
  const last = seconds.at(-1);
  if (first === undefined || last === undefined || last <= first) {
    return seconds.map((_, at) => (seconds.length < 2 ? 0 : at / (seconds.length - 1)));
  }
  return seconds.map(each => (each - first) / (last - first));
}

/**
 * A length of running time in words, the one way the screen says it: the time bar's word and the
 * caption of every chart. Floored, because a clock never says a time it has not reached: to the
 * second below an hour (`21m 40s`), to the minute from an hour on (`11h 17m`).
 */
export function runningWords(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m ${String(whole % 60).padStart(2, '0')}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}
