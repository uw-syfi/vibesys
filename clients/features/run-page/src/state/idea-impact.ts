/**
 * What each idea did to each objective: its reading, its multiple of the input, whether it set a new
 * best, and what share of the objective's gain that was. Every figure is `objective-reading`'s: the
 * multiple of an idea is `multipleOver` its reading and the run's one input, the ideas that set a
 * best are the owners of the steps of `bestSteps`, so a row of the Ideas list and the chart's
 * headline cannot state two multiples of one input or name two bests.
 *
 * The impact is read from the whole run, cut at the time bar's position (`until`: readings taken
 * and steps set at or before it) exactly as the chart is. It is never read from the run as it stood
 * at that position: that run holds the writer's series without its decisions (no best flags, no
 * stated value) and without the input of an objective other than the first.
 *
 * Share of gain, for a run with one objective and a known input: the best-so-far line goes from the
 * input to its last step in steps, each set by an idea; the idea's share is the sum of the
 * improvements of its steps over the best before them, divided by the improvement of the whole line.
 * The steps' improvements telescope to the line's, so the shares of the ideas that set a step, and
 * of the steps no idea owns (`unowned`), sum to one. Without the input the line has no start, so
 * there are no shares.
 *
 * Pure over the readings; nothing here words anything.
 */
import type {Direction} from './direction.js';
import {type Change, changeOf} from './headline.js';
import {
  type BestReading,
  multipleOver,
  type ObjectiveReading,
  type ObjectiveSeries,
  objectiveSeriesOf,
  readingKey,
  readingsUntil,
  readObjective,
  stepsUntil,
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

/**
 * The reading each idea stands for among `points`: the last one that set a step of the line, else
 * its last, the one the chart draws for it.
 */
function pointsByIdea(
  points: readonly PerformancePoint[],
  steps: readonly BestReading[],
): ReadonlyMap<string, PerformancePoint> {
  const stepKeys = new Set(steps.flatMap(step => (step.key === null ? [] : [step.key])));
  const found = new Map<string, PerformancePoint>();
  for (const point of points) {
    if (point.unowned === true) continue;
    const known = found.get(point.id);
    if (
      known === undefined ||
      stepKeys.has(readingKey(point)) ||
      !stepKeys.has(readingKey(known))
    ) {
      found.set(point.id, point);
    }
  }
  return found;
}

/**
 * The improvement each step of the line added over the one before it (the first over the input),
 * with the idea that set it; the first step's is null when the input is unknown.
 */
function gainsOf(
  reading: ObjectiveReading,
  steps: readonly BestReading[],
): readonly {readonly idea: string | null; readonly gain: number | null}[] {
  let from = reading.input;
  return steps.map(step => {
    const gain = from === null ? null : improvementOf(step.value, from, reading.direction);
    from = step.value;
    return {idea: step.idea, gain};
  });
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
  steps: ReadonlySet<string>,
  gain: number | null,
): ObjectiveCell {
  if (point === undefined) return NO_CELL;
  const multiple = multipleOver(point.value, reading.input, reading.direction);
  const setBest = steps.has(readingKey(point));
  return {
    value: point.value,
    multiple,
    change: multiple === null ? null : changeOf(multiple),
    setBest,
    gain: setBest ? gain : null,
  };
}

/** The shares of the line's gain by idea, and what no idea owns; null without an input or a gain to share. */
function sharesOf(
  reading: ObjectiveReading,
  steps: readonly BestReading[],
): {readonly byIdea: ReadonlyMap<string, number>; readonly unowned: number} | null {
  if (reading.input === null) return null;
  const gains = gainsOf(reading, steps).map(each => ({idea: each.idea, gain: each.gain ?? 0}));
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

/** What the ideas did to `readings` (the run's objectives, read) by `until`, in the readings' order. */
function impactOfReadings(readings: readonly ObjectiveReading[], until: number | null): Impact {
  const lines = readings.map(reading => stepsUntil(reading, until));
  const points = readings.map((reading, at) =>
    pointsByIdea(readingsUntil(reading, until), lines[at] ?? []),
  );
  const stepKeys = lines.map(
    line => new Set(line.flatMap(step => (step.key === null ? [] : [step.key]))),
  );
  const gainsByIdea = readings.map((reading, at) => {
    const byIdea = new Map<string, number>();
    for (const {idea, gain} of gainsOf(reading, lines[at] ?? [])) {
      if (idea !== null && gain !== null) byIdea.set(idea, (byIdea.get(idea) ?? 0) + gain);
    }
    return byIdea;
  });
  const ids = new Set(points.flatMap(each => [...each.keys()]));
  const cells = new Map<string, readonly ObjectiveCell[]>();
  for (const id of ids) {
    cells.set(
      id,
      readings.map((reading, at) =>
        cellOf(
          reading,
          points[at]?.get(id),
          stepKeys[at] ?? new Set(),
          gainsByIdea[at]?.get(id) ?? null,
        ),
      ),
    );
  }
  const [only] = readings;
  const [line] = lines;
  const shared = readings.length === 1 && only !== undefined ? sharesOf(only, line ?? []) : null;
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
 * What the ideas of the run `whole` (to its end, whatever the position of the time bar) did to its
 * objectives `objectives`, as of run-clock time `until` (null: the end): the readings taken and the
 * steps set by then. The columns are the whole run's, so scrubbing neither adds nor drops one.
 */
export function impactOf(
  whole: RunModel,
  objectives: readonly ObjectiveSeries[] = objectiveSeriesOf(whole),
  until: number | null = null,
): Impact {
  const series = objectives.length > 0 ? objectives : objectiveSeriesOf(whole);
  return impactOfReadings(
    series.map((each, at) => readObjective(whole, each, at === 0, series.length < 2)),
    until,
  );
}
