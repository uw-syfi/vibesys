/**
 * The performance of a run, objective by objective, as the orchestrator serves it: every measured
 * candidate, the points that set a new best (the best-so-far line), the standing bound with its
 * provenance, the head's value and the percent of the bound the head and the best are at. Nothing
 * here is worked out: the line's marks come from the search state, and every figure the header and
 * tiles show (best, head, unit, percents) is the objective's served standing, so this only changes
 * shapes (bigints and timestamps become numbers) and joins what one objective has under its name.
 */
import {
  BoundUnitCheck,
  Direction,
  type ObjectiveStanding,
  type SearchState,
} from '@vibesys/orchestrator-proto';
import {type ObjectiveBoundsView, objectiveBounds} from '../search-views.js';
import {ms} from '../time.js';

/** Which way along the value is better. */
export type Better = 'higher' | 'lower';

/** One measurement of one candidate on the objective. */
export interface MeasuredPointView {
  /** The measure job that made it: the identity a candidate's detail is found by. */
  readonly id: string;
  readonly commit: string;
  readonly atMs: number;
  readonly value: number;
  /** The host says it beat every earlier point: the best-so-far line steps here. */
  readonly bestSoFar: boolean;
}

export interface ObjectiveReport {
  readonly name: string;
  /** Which way improves, or null when only the summary declares the objective. */
  readonly better: Better | null;
  /** What the value is counted in, as the standing says; null when it says none. */
  readonly unit: string | null;
  /** In the order the host sent them: oldest first. */
  readonly points: readonly MeasuredPointView[];
  /** The points that set a best, in order: the corners of the best-so-far line. */
  readonly bestPath: readonly MeasuredPointView[];
  /**
   * The best the run measured, as the objective's standing serves it (the bound's best when there is
   * one). The series only draws the line: its newest corner is not read for this.
   */
  readonly bestValue: number | undefined;
  /** The head's value on the objective, as the host served it; undefined when it has none. */
  readonly headValue: number | undefined;
  /** How close the head is to the standing bound in percent, as the host worked it out. */
  readonly percentOfBound: number | undefined;
  /** How close the best measured value (`best`) is to the standing bound, as the host worked it out. */
  readonly bestPercentOfBound: number | undefined;
  readonly bound: ObjectiveBoundsView | undefined;
  /** The unit the standing bound is counted in, as served; null when none. */
  readonly boundUnit: string | null;
  /** Whether the bound's unit is the values' unit, as the host checked it. */
  readonly boundUnitCheck: BoundUnitCheck;
}

export const betterOf = (direction: Direction): Better =>
  direction === Direction.MINIMIZE ? 'lower' : 'higher';

const unitOf = (standing: ObjectiveStanding | undefined): string | null =>
  standing === undefined || standing.unit === '' ? null : standing.unit;

const boundUnitOf = (
  standing: ObjectiveStanding | undefined,
): Pick<ObjectiveReport, 'boundUnit' | 'boundUnitCheck'> => ({
  boundUnit: standing === undefined || standing.boundUnit === '' ? null : standing.boundUnit,
  boundUnitCheck: standing?.boundUnitCheck ?? BoundUnitCheck.UNSPECIFIED,
});

/**
 * The objectives of the run in the summary's order (the task's own), then measured objectives in
 * their served order, then objectives that only have a bound, by name. The summary and search are
 * disjoint feeds, so either can be absent without hiding what the other has already served.
 */
export function objectiveReports(
  search: SearchState | undefined,
  standings: readonly ObjectiveStanding[],
): readonly ObjectiveReport[] {
  const bounds = new Map(
    (search === undefined ? [] : objectiveBounds(search)).map(bound => [bound.objective, bound]),
  );
  const standingOf = new Map(standings.map(standing => [standing.name, standing]));
  const series = search?.series ?? [];
  const seriesOf = new Map(series.map(entry => [entry.objective, entry]));
  const seen = new Set<string>();
  const names = [
    ...standings.map(standing => standing.name),
    ...series.map(entry => entry.objective),
    ...[...bounds.keys()].sort((a, b) => a.localeCompare(b)),
  ].filter(name => {
    if (seen.has(name)) return false;
    seen.add(name);
    return true;
  });
  return names.map((name): ObjectiveReport => {
    const objectiveSeries = seriesOf.get(name);
    const points = (objectiveSeries?.points ?? []).map(point => ({
      id: point.jobId,
      commit: point.commit,
      atMs: ms(point.at) ?? 0,
      value: point.value,
      bestSoFar: point.bestSoFar,
    }));
    const bestPath = points.filter(point => point.bestSoFar);
    const bound = bounds.get(name);
    const standing = standingOf.get(name);
    return {
      name,
      better:
        objectiveSeries !== undefined
          ? betterOf(objectiveSeries.direction)
          : bound === undefined
            ? null
            : betterOf(bound.direction),
      unit: unitOf(standing),
      points,
      bestPath,
      bestValue: standing?.bestValue,
      headValue: standing?.headValue,
      percentOfBound: standing?.headPercentOfBound,
      bestPercentOfBound: standing?.bestPercentOfBound,
      bound,
      ...boundUnitOf(standing),
    };
  });
}
