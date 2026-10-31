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
import {
  ideaReading,
  inputValueOf,
  type PerformanceFold,
  type PerformancePoint,
  performanceFold,
} from './performance.js';
import type {RunModel} from './run-model.js';

/** A best of an objective: the value, the idea that set it, the reading it is, and when. */
export interface BestReading {
  readonly value: number;
  /** The idea whose reading set it; null when the reading has no idea to open. */
  readonly idea: string | null;
  /** The reading that set it (its `key`, else its idea's id); null when no reading of the run is it. */
  readonly key: string | null;
  /** Run-clock seconds of the reading. */
  readonly at: number;
}

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
  /**
   * The best the run's writer states for the objective when the run ends or is read at its end;
   * null when it states none (a run read from events, or a read before the writer committed).
   */
  readonly stated: number | null;
  /**
   * The best the run's own decision names, for a run read from events: the idea its adoption (or,
   * while it works, its best so far) names, or null when the input stands. Absent for a run whose
   * writer states its series, which states its best itself (`stated`).
   */
  readonly decided?: BestReading | null;
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

/**
 * Whether the run has decided its best: its adoption span is open and names a leader or the input.
 * Until then (an adoption that says the input stands while the run still works is no decision) its
 * best is the best so far, which is what `withLiveBest` shows its Ideas list.
 */
function decides(model: RunModel): boolean {
  const {adoptionOpened, best} = model.candidates;
  return adoptionOpened && !(best !== null && best.candidate === null && !best.adopted);
}

/** The idea the run's own decision names as its best, with its value and when it was read. */
function decidedBest(
  model: RunModel,
  series: ObjectiveSeries,
  fold: PerformanceFold,
): BestReading | null {
  const id = model.candidates.best?.candidate ?? null;
  const item = model.candidates.candidates.find(each => each.id === id);
  const value = item === undefined ? null : ideaReading(item, series.name);
  if (item === undefined || value === null) return null;
  const point = fold.points.find(each => each.id === item.id);
  return {
    value,
    idea: item.id,
    key: point === undefined ? null : readingKey(point),
    at: point?.at ?? item.measurement?.at ?? item.startedAt,
  };
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

/** The best the writer states for `series`: its headline's for the objective the headline is of. */
function statedBest(
  model: RunModel,
  series: ObjectiveSeries,
  primary: boolean,
  states: PerformanceFold | undefined,
): number | null {
  if (model.series === undefined) return null;
  const headline = model.headline;
  const writers =
    headline !== undefined &&
    (headline.series === undefined ? primary : headline.series.name === series.name);
  return (writers ? headline?.bestValue : states?.value) ?? null;
}

/**
 * `series` of `model` (`alone`: the run has this one objective). A run that states its series (the
 * writer's) reads each from its own states; a run read from events has the readings of its ideas for
 * its one objective only: with several, no idea's reading stands in for a series nobody stated.
 */
export function readObjective(
  model: RunModel,
  series: ObjectiveSeries,
  primary: boolean,
  alone = true,
): ObjectiveReading {
  const states = series.name === null ? undefined : model.series?.get(series.name);
  const input = inputOf(model, series, primary, states);
  const fold =
    states ??
    (model.series === undefined && primary && alone
      ? performanceFold(model.candidates.candidates, series.direction, input, series.name)
      : NOTHING_READ);
  const stated = statedBest(model, series, primary, states);
  const read = {...series, primary, fold, input, stated};
  return model.series === undefined && primary && alone && decides(model)
    ? {...read, decided: decidedBest(model, series, fold)}
    : read;
}

/** Every objective of `model`, read. */
export function readObjectives(model: RunModel): readonly ObjectiveReading[] {
  const all = objectiveSeriesOf(model);
  return all.map((series, at) => readObjective(model, series, at === 0, all.length < 2));
}

/** The identity of a reading: its own key when the run keeps several of one idea, else its idea's id. */
export function readingKey(point: PerformancePoint): string {
  return point.key ?? point.id;
}

/** `point` as a best: the reading that set it, with its idea when it has one to open. */
function bestOf(point: PerformancePoint): BestReading {
  return {
    value: point.value,
    idea: point.unowned === true ? null : point.id,
    key: readingKey(point),
    at: point.at,
  };
}

/**
 * The best of the whole run: what the run decided, or its writer states, else the last reading that
 * set a new best; null when the input stands. A stated best is placed at the reading that has its
 * value (one that set a best first), else at the last reading.
 */
function wholeBest(
  reading: ObjectiveReading,
  flagged: readonly PerformancePoint[],
): BestReading | null {
  if (reading.decided !== undefined) return reading.decided;
  const last = flagged.at(-1);
  if (reading.stated === null) return last === undefined ? null : bestOf(last);
  const value = reading.stated;
  const holder =
    flagged.filter(point => point.value === value).at(-1) ??
    reading.fold.points.filter(point => point.value === value).at(-1);
  if (holder !== undefined) return bestOf(holder);
  return {value, idea: null, key: null, at: reading.fold.points.at(-1)?.at ?? 0};
}

/** Whether the run says nothing beat the input: it decided for it, or its writer states it as the best. */
function inputStands(reading: ObjectiveReading): boolean {
  return (
    reading.decided === null ||
    (reading.decided === undefined && reading.stated !== null && reading.stated === reading.input)
  );
}

/**
 * The steps of the best-so-far line over the whole run, in order: each reading that set a new best,
 * up to the best of the whole run, which is the line's last value whatever states it: the run's
 * decision, its writer's statement, or its last new best. A decision for an idea (or a statement of
 * a value) its readings did not flag as a new best adds that reading as the last step, and the steps
 * after it are not on the line; a run that keeps the input has no step. This is the one place the
 * line and the headline are decided: `bestSoFar` is the last of these steps by a time.
 */
export function bestSteps(reading: ObjectiveReading): readonly BestReading[] {
  if (inputStands(reading)) return [];
  const flagged = reading.fold.points.filter(point => point.best);
  const end = wholeBest(reading, flagged);
  if (end === null) return [];
  const upTo = flagged.filter(point => point.at <= end.at).map(bestOf);
  return upTo.at(-1)?.value === end.value ? upTo : [...upTo, end];
}

/**
 * The best of `reading` as of run-clock time `until` (null: the whole run): the last step of its
 * line by then (`bestSteps`), so the headline, the line and the tip name one best. Null when none
 * has been set, so the input stands; but a writer that states the input as the run's best says so
 * for the whole run: that is the best, with no step.
 */
export function bestSoFar(reading: ObjectiveReading, until: number | null): BestReading | null {
  const last = bestSteps(reading)
    .filter(step => until === null || step.at <= until)
    .at(-1);
  if (last !== undefined || until !== null) return last ?? null;
  return reading.decided === undefined && reading.stated !== null
    ? {value: reading.stated, idea: null, key: null, at: 0}
    : null;
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
    const value = ideaReading(idea, first.name);
    return value === null ? null : multipleOver(value, input, first.direction);
  };
}
