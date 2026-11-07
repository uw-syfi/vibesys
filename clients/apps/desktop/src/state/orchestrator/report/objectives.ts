/**
 * The performance of a run, objective by objective, as the orchestrator serves it: every measured
 * candidate, the points that set a new best (the best-so-far line), the standing bound with its
 * provenance, and the head's value and its percent of the bound. Nothing here is worked out: the
 * best marks and the percent come from the host, so this only changes their shape (bigints and
 * timestamps become numbers) and joins what one objective has under its name.
 */
import {Direction, oneofIs, type SearchState} from '@vibesys/orchestrator-proto';
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
  /** What the value is counted in, as the agent that declared the bound said; null with no bound. */
  readonly unit: string | null;
  /** In the order the host sent them: oldest first. */
  readonly points: readonly MeasuredPointView[];
  /** The points that set a best, in order: the corners of the best-so-far line. */
  readonly bestPath: readonly MeasuredPointView[];
  /** The newest point that set a best, or undefined with no measurement. */
  readonly best: MeasuredPointView | undefined;
  /** The head's value on the objective, as the host read it; undefined when it has none. */
  readonly headValue: number | undefined;
  /** How close the head is to the standing bound in percent, as the host worked it out. */
  readonly percentOfBound: number | undefined;
  readonly bound: ObjectiveBoundsView | undefined;
}

export const betterOf = (direction: Direction): Better =>
  direction === Direction.MINIMIZE ? 'lower' : 'higher';

/** The head's own reading of an objective, when the bound view carries none. */
function headReading(search: SearchState, name: string): number | undefined {
  const head = search.head?.state;
  const result = oneofIs(head, 'current', {current: true, none: true})
    ? head.value.result?.outcome
    : undefined;
  const measure = oneofIs(result, 'measure', {check: true, measure: true})
    ? result.value
    : undefined;
  if (measure === undefined) return undefined;
  if (measure.metric?.name === name) return measure.value;
  return measure.observations.find(observation => observation.name === name)?.value;
}

/**
 * The objectives of the run in the order the host serves them (the task's own), then the ones that
 * have a declared bound and no measurement yet, by name.
 */
export function objectiveReports(search: SearchState): readonly ObjectiveReport[] {
  const bounds = new Map(objectiveBounds(search).map(bound => [bound.objective, bound]));
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
      unit: bound?.current.unit ?? null,
      points,
      bestPath,
      best: bestPath.at(-1),
      headValue: bound?.headValue ?? headReading(search, series.objective),
      percentOfBound: bound?.percentOfBound,
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
      unit: bound.current.unit,
      points: [],
      bestPath: [],
      best: undefined,
      headValue: bound.headValue,
      percentOfBound: bound.percentOfBound,
      bound,
    }));
  return [...measured, ...unmeasured];
}
