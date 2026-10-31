/**
 * What each idea did to each objective: its reading, its multiple of the input, whether it set a new
 * best, and what share of the objective's gain that was. Every figure is `objective-reading`'s: the
 * multiple of an idea is `multipleOver` its reading and the run's one input, the idea that set the
 * best is `bestSoFar`'s, so a row of the Ideas list and the chart's headline cannot state two
 * multiples of one input or name two bests.
 *
 * Share of gain, for a run with one objective and a known input: the best-so-far line goes from the
 * input to its last step in steps, each set by an idea; the idea's share is the sum of the
 * improvements of its steps over the best before them, divided by the improvement of the whole line.
 * The steps' improvements telescope to the line's, so the shares of the ideas that set a step, and
 * of the steps no idea owns (`unowned`), sum to one.
 *
 * Pure over the readings; nothing here words anything.
 */
import type {Direction} from './direction.js';
import {type Change, changeOf} from './headline.js';
import {
  bestSteps,
  multipleOver,
  type ObjectiveReading,
  type ObjectiveSeries,
  objectiveSeriesOf,
  readObjective,
} from './objective-reading.js';
import type {PerformancePoint} from './performance.js';
import type {RunModel} from './run-model.js';

/** One idea's reading of one objective. */
export interface ObjectiveCell {
  readonly value: number | null;
  /** The reading as an improvement over the run's input (`2` is twice as good); null without either. */
  readonly multiple: number | null;
  readonly change: Change | null;
  /** The reading set a new best of the objective. */
  readonly setBest: boolean;
  /** The improvement the reading added over the best standing before it; null unless it set a best. */
  readonly gain: number | null;
}

/** An objective as a column of the list. */
export interface ObjectiveColumn extends ObjectiveSeries {
  readonly input: number | null;
  /** Some reading differs from the input: an objective that never moved has no column (its chart stays). */
  readonly moved: boolean;
}

export interface Impact {
  /** One per objective, in the run's order. */
  readonly columns: readonly ObjectiveColumn[];
  /** Each idea with a reading: its cell per column, in the columns' order. */
  readonly cells: ReadonlyMap<string, readonly ObjectiveCell[]>;
  /**
   * Each idea's share of the gain (0 to 1) of a run with one objective whose input is known and
   * whose best line gained; null when there is no such share to tell.
   */
  readonly shares: ReadonlyMap<string, number> | null;
  /** The share of the steps that no idea set (their reading has no idea to open); 0 when owned. */
  readonly unowned: number;
}

const NO_CELL: ObjectiveCell = {
  value: null,
  multiple: null,
  change: null,
  setBest: false,
  gain: null,
};

/** The improvement of `value` over `from`: positive where `direction` says it is better. */
function improvementOf(value: number, from: number, direction: Direction): number {
  return direction === 'min' ? from - value : value - from;
}

/** The points of `reading` by idea: the one that set a best, else the first read. */
function pointsByIdea(reading: ObjectiveReading): ReadonlyMap<string, PerformancePoint> {
  const found = new Map<string, PerformancePoint>();
  for (const point of reading.fold.points) {
    if (point.unowned === true) continue;
    const known = found.get(point.id);
    if (known === undefined || (point.best && !known.best)) found.set(point.id, point);
  }
  return found;
}

/** The improvements of the steps of `reading`'s best line, each with the idea that set it. */
function gainsOf(
  reading: ObjectiveReading,
): readonly {readonly idea: string | null; readonly gain: number}[] {
  let from = reading.input;
  const gains: {idea: string | null; gain: number}[] = [];
  for (const step of bestSteps(reading)) {
    if (from !== null) {
      gains.push({idea: step.idea, gain: improvementOf(step.value, from, reading.direction)});
    }
    from = step.value;
  }
  return gains;
}

/** Whether any reading of `reading` is not the input's: an objective that never moved has none. */
function moves(reading: ObjectiveReading): boolean {
  const values = reading.fold.points.map(point => point.value);
  if (reading.input === null) return new Set(values).size > 1;
  const input = reading.input;
  return values.some(value => {
    const multiple = multipleOver(value, input, reading.direction);
    return multiple === null ? value !== input : changeOf(multiple) !== 'same';
  });
}

function cellOf(
  reading: ObjectiveReading,
  point: PerformancePoint | undefined,
  gain: number | null,
): ObjectiveCell {
  if (point === undefined) return NO_CELL;
  const multiple = multipleOver(point.value, reading.input, reading.direction);
  return {
    value: point.value,
    multiple,
    change: multiple === null ? null : changeOf(multiple),
    setBest: point.best,
    gain: point.best ? gain : null,
  };
}

/** The shares of `reading`'s gain by idea, and what no idea owns; null when it has no gain to share. */
function sharesOf(
  reading: ObjectiveReading,
): {readonly byIdea: ReadonlyMap<string, number>; readonly unowned: number} | null {
  const gains = gainsOf(reading);
  const total = gains.reduce((sum, each) => sum + each.gain, 0);
  if (gains.length === 0 || !(total > 0)) return null;
  const byIdea = new Map<string, number>();
  let unowned = 0;
  for (const {idea, gain} of gains) {
    if (idea === null) unowned += gain / total;
    else byIdea.set(idea, (byIdea.get(idea) ?? 0) + gain / total);
  }
  return {byIdea, unowned};
}

/** What the ideas did to `readings` (the run's objectives, read), in the readings' order. */
export function impactOfReadings(readings: readonly ObjectiveReading[]): Impact {
  const points = readings.map(pointsByIdea);
  const gainsByIdea = readings.map(reading => {
    const byIdea = new Map<string, number>();
    for (const {idea, gain} of gainsOf(reading)) {
      if (idea !== null) byIdea.set(idea, (byIdea.get(idea) ?? 0) + gain);
    }
    return byIdea;
  });
  const ids = new Set(points.flatMap(each => [...each.keys()]));
  const cells = new Map<string, readonly ObjectiveCell[]>();
  for (const id of ids) {
    cells.set(
      id,
      readings.map((reading, at) =>
        cellOf(reading, points[at]?.get(id), gainsByIdea[at]?.get(id) ?? null),
      ),
    );
  }
  const [only] = readings;
  const shared = readings.length === 1 && only !== undefined ? sharesOf(only) : null;
  return {
    columns: readings.map(reading => ({
      name: reading.name,
      direction: reading.direction,
      input: reading.input,
      moved: moves(reading),
    })),
    cells,
    shares: shared === null ? null : shared.byIdea,
    unowned: shared === null ? 0 : shared.unowned,
  };
}

/**
 * What the ideas of `model` did to the objectives `objectives` (the run's, read from the whole run
 * so a scrubbed model keeps its columns), at the sequence `model` is at.
 */
export function impactOf(
  model: RunModel,
  objectives: readonly ObjectiveSeries[] = objectiveSeriesOf(model),
): Impact {
  const series = objectives.length > 0 ? objectives : objectiveSeriesOf(model);
  return impactOfReadings(
    series.map((each, at) => readObjective(model, each, at === 0, series.length < 2)),
  );
}
