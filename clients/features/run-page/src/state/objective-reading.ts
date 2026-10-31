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
import {ideaReading, inputValueOf, type PerformanceFold, performanceFold} from './performance.js';
import type {RunModel} from './run-model.js';

/** An objective's best: the value, the idea that set it, and when. */
export interface BestReading {
  readonly value: number;
  /** The idea whose reading set it; null when the reading has no idea to open. */
  readonly idea: string | null;
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
  return {value, idea: item.id, at: point?.at ?? item.measurement?.at ?? item.startedAt};
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

/**
 * The best of `reading` as of run-clock time `until` (null: the whole run): what the run decided or
 * its writer states for the whole run, else the last reading that set a new best by then. Null when none has, so the
 * input stands.
 */
export function bestSoFar(reading: ObjectiveReading, until: number | null): BestReading | null {
  if (until === null && reading.decided !== undefined) return reading.decided;
  const set = reading.fold.points.filter(
    point => point.best && (until === null || point.at <= until),
  );
  const last = set.at(-1);
  const setter = last === undefined || last.unowned === true ? null : last.id;
  if (until === null && reading.stated !== null) {
    return {
      value: reading.stated,
      idea: last?.value === reading.stated ? setter : null,
      at: last?.at ?? 0,
    };
  }
  return last === undefined ? null : {value: last.value, idea: setter, at: last.at};
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

/** A step of the best-so-far line: the value from the time it was read, and the idea that set it. */
export interface BestStep {
  readonly at: number;
  readonly value: number;
  readonly idea: string | null;
}

/**
 * The steps of the best-so-far line over the whole run, in order: each reading that set a new best,
 * up to the best of the whole run (`bestSoFar`), which is the line's last value. A run whose own
 * decision keeps the input has no step; a decision for an idea its readings did not flag as a new
 * best adds that idea's reading as the last step.
 */
export function bestSteps(reading: ObjectiveReading): readonly BestStep[] {
  const flagged = reading.fold.points
    .filter(point => point.best)
    .map(point => ({
      at: point.at,
      value: point.value,
      idea: point.unowned === true ? null : point.id,
    }));
  const decided = reading.decided;
  if (decided === undefined) return flagged;
  if (decided === null) return [];
  const upTo = flagged.filter(step => step.at <= decided.at);
  const last = upTo.at(-1);
  return last?.value === decided.value
    ? upTo
    : [...upTo, {at: decided.at, value: decided.value, idea: decided.idea}];
}
