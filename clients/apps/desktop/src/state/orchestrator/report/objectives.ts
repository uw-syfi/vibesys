/**
 * The performance of a run, objective by objective, as the orchestrator serves it: every measured
 * candidate, the points that set a new best (the best-so-far line), the standing bound with its
 * provenance, the head's value and the percent of the bound the head and the best are at. Nothing
 * here is worked out: the line's marks come from the search state, and every figure the header and
 * tiles show (best, head, unit, percents) is the objective's served standing, so this only changes
 * shapes (bigints and timestamps become numbers) and joins what one objective has under its name.
 */
import {Direction, type ObjectiveStanding, type SearchState} from '@vibesys/orchestrator-proto';
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
  readonly better: Better;
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
}

export const betterOf = (direction: Direction): Better =>
  direction === Direction.MINIMIZE ? 'lower' : 'higher';

const unitOf = (standing: ObjectiveStanding | undefined): string | null =>
  standing === undefined || standing.unit === '' ? null : standing.unit;

/**
 * The objectives of the run in the order the host serves them (the task's own), then the ones that
 * have a declared bound and no measurement yet, by name.
 */
export function objectiveReports(
  search: SearchState,
  standings: readonly ObjectiveStanding[],
): readonly ObjectiveReport[] {
  const bounds = new Map(objectiveBounds(search).map(bound => [bound.objective, bound]));
  const standingOf = new Map(standings.map(standing => [standing.name, standing]));
  const measured: ObjectiveReport[] = search.series.map(series => {
    const points = series.points.map(point => ({
      id: point.jobId,
      commit: point.commit,
      atMs: ms(point.at) ?? 0,
      value: point.value,
      bestSoFar: point.bestSoFar,
    }));
    const bestPath = points.filter(point => point.bestSoFar);
    const bound = bounds.get(series.objective);
    return {
      name: series.objective,
      better: betterOf(series.direction),
      unit: unitOf(standingOf.get(series.objective)),
      points,
      bestPath,
      bestValue: standingOf.get(series.objective)?.bestValue,
      headValue: standingOf.get(series.objective)?.headValue,
      percentOfBound: standingOf.get(series.objective)?.headPercentOfBound,
      bestPercentOfBound: standingOf.get(series.objective)?.bestPercentOfBound,
      bound,
    };
  });
  const seen = new Set(measured.map(objective => objective.name));
  const unmeasured: ObjectiveReport[] = [...bounds.values()]
    .filter(bound => !seen.has(bound.objective))
    .sort((a, b) => a.objective.localeCompare(b.objective))
    .map(bound => ({
      name: bound.objective,
      better: betterOf(bound.direction),
      unit: unitOf(standingOf.get(bound.objective)),
      points: [],
      bestPath: [],
      bestValue: standingOf.get(bound.objective)?.bestValue,
      headValue: standingOf.get(bound.objective)?.headValue,
      percentOfBound: standingOf.get(bound.objective)?.headPercentOfBound,
      bestPercentOfBound: standingOf.get(bound.objective)?.bestPercentOfBound,
      bound,
    }));
  return [...measured, ...unmeasured];
}
