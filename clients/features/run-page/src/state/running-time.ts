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
