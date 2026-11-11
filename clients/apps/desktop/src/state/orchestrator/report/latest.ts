/**
 * The newest measurements of a run as rows of a table: when, which idea, one value for each
 * objective, and the commit. A measure job is one row however many objectives it measured; the
 * value a measurement set a new best with is marked by the host's own `best_so_far`. Nothing is
 * compared here: rows are picked by their recorded time, and a value is the one the host served.
 */
import type {ObjectiveReport} from './objectives.js';

/** One measure job's value on one objective. */
export interface LatestValue {
  readonly value: number;
  /** The host says it beat every earlier measurement of the objective. */
  readonly newBest: boolean;
}

export interface LatestMeasurement {
  /** The measure job. */
  readonly id: string;
  readonly atMs: number;
  readonly commit: string;
  /** The plans the job names: none for a `--no-plan` job; undefined while no ideas are served. */
  readonly plans: readonly number[] | undefined;
  /** One for each objective, in the order of `objectives`; null where the job has no value for it. */
  readonly values: readonly (LatestValue | null)[];
}

/** How many measurements the report's table lists. */
export const LATEST_COUNT = 8;

/** The newest `limit` measure jobs of the objectives, newest first. */
export function latestMeasurements(
  objectives: readonly ObjectiveReport[],
  plansOfJob: ReadonlyMap<string, readonly number[]> | undefined,
  limit: number = LATEST_COUNT,
): readonly LatestMeasurement[] {
  const jobs = new Map<string, {atMs: number; commit: string; values: (LatestValue | null)[]}>();
  objectives.forEach((objective, at) => {
    for (const point of objective.points) {
      const job = jobs.get(point.id) ?? {
        atMs: point.atMs,
        commit: point.commit,
        values: objectives.map(() => null),
      };
      job.values[at] = {value: point.value, newBest: point.bestSoFar};
      jobs.set(point.id, job);
    }
  });
  return [...jobs.entries()]
    .sort(([aId, a], [bId, b]) => b.atMs - a.atMs || (aId < bId ? 1 : aId > bId ? -1 : 0))
    .slice(0, Math.max(0, limit))
    .map(([id, job]) => ({id, ...job, plans: plansOfJob?.get(id)}));
}
