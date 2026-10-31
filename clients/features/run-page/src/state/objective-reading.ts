/**
 * An objective as every face reads it: the readings it was measured at, the input's value, its best
 * so far, and the multiple of the input that best makes. The chart's headline, the end of its
 * best-so-far line, and the `vs input` of an idea's row are all this one reading, so they cannot
 * name different bests or different inputs: the input is the run's one input, not the one each
 * idea's own measurement was compared with, and the multiple is the improvement over it where the
 * objective's direction says which way is better.
 *
 * Pure over the run model; nothing here words anything.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {improvement, objectiveMetric} from './baselines.js';
import type {Direction} from './direction.js';
import {objectivesOf} from './pareto-view.js';
import {inputValueOf, type PerformanceFold, performanceFold} from './performance.js';
import type {RunModel} from './run-model.js';

/** An objective of the run: the series' name (null before the run names one) and its direction. */
export interface ObjectiveSeries {
  readonly name: string | null;
  readonly direction: Direction;
}

/** An objective's readings and input. */
export interface ObjectiveReading extends ObjectiveSeries {
  /** Whether this is the run's first objective, the one an idea's headline measure is. */
  readonly primary: boolean;
  /** Every reading with the ones that set a new best (`PerformancePoint.best`), in time order. */
  readonly fold: PerformanceFold;
  /** The run's input value of the objective; null when the run has no measurement of it. */
  readonly input: number | null;
}

const NOTHING_READ: PerformanceFold = {points: [], best: []};

/**
 * The objectives of `model` in the run's order: those its overview declares, else the one metric the
 * overview names (null before it names any), with the model's direction.
 */
export function objectiveSeriesOf(model: RunModel): readonly ObjectiveSeries[] {
  const declared = objectivesOf(model);
  return declared.length > 0
    ? declared
    : [{name: objectiveMetric(model), direction: model.direction}];
}

/** The input's value of `series`, from the run's own states of it, else from what its ideas measured. */
function inputOf(
  model: RunModel,
  series: ObjectiveSeries,
  primary: boolean,
  states: PerformanceFold | undefined,
): number | null {
  if (model.series !== undefined) {
    return states?.input ?? (primary ? (model.headline?.inputValue ?? model.input ?? null) : null);
  }
  const candidates = model.candidates.candidates;
  const named =
    candidates
      .flatMap(idea => idea.measurement?.inputReadings ?? [])
      .find(reading => reading.name === series.name)?.value ?? null;
  return primary ? (inputValueOf(candidates) ?? named ?? model.input ?? null) : named;
}

/**
 * `series` of `model`. A run that states its series (the writer's) reads each from its own states; a
 * run read from events has the readings of its ideas for its first objective only, as an idea's
 * measurement carries no other.
 */
export function readObjective(
  model: RunModel,
  series: ObjectiveSeries,
  primary: boolean,
): ObjectiveReading {
  const states = series.name === null ? undefined : model.series?.get(series.name);
  const input = inputOf(model, series, primary, states);
  const fold =
    states ??
    (model.series === undefined && primary
      ? performanceFold(model.candidates.candidates, series.direction, input)
      : NOTHING_READ);
  return {...series, primary, fold, input};
}

/** Every objective of `model`, read. */
export function readObjectives(model: RunModel): readonly ObjectiveReading[] {
  return objectiveSeriesOf(model).map((series, at) => readObjective(model, series, at === 0));
}

/** An objective's best: the value, the idea that set it, and when. */
export interface BestReading {
  readonly value: number;
  /** The idea whose reading set it; null when the reading has no idea to open. */
  readonly idea: string | null;
  /** Run-clock seconds of the reading. */
  readonly at: number;
}

/**
 * The best of `reading` as of run-clock time `until` (null: the whole run): the last reading that
 * set a new best by then. Null when none has, so the input stands.
 */
export function bestSoFar(reading: ObjectiveReading, until: number | null): BestReading | null {
  const set = reading.fold.points.filter(
    point => point.best && (until === null || point.at <= until),
  );
  const last = set.at(-1);
  if (last === undefined) return null;
  return {value: last.value, idea: last.unowned === true ? null : last.id, at: last.at};
}

/**
 * `value` as an improvement over `input`, where `direction` says which way is better (`2` is twice
 * as good, whichever way the metric runs); null when there is no ratio to take.
 */
export function multipleOver(
  value: number,
  input: number | null,
  direction: Direction,
): number | null {
  if (input === null || input === 0 || (direction === 'min' && value === 0)) return null;
  return improvement(value / input, direction);
}

/**
 * The `vs input` of an idea: its measured value as a multiple of the run's input, the same reading
 * as the chart's headline; null when the idea measured nothing or the run has no input.
 */
export function vsInputOf(model: RunModel): (idea: Candidate) => number | null {
  const [first] = objectiveSeriesOf(model);
  if (first === undefined) return () => null;
  const states = first.name === null ? undefined : model.series?.get(first.name);
  const input = inputOf(model, first, true, states);
  return idea => {
    const value = idea.measurement?.metricValue ?? null;
    return value === null ? null : multipleOver(value, input, first.direction);
  };
}
