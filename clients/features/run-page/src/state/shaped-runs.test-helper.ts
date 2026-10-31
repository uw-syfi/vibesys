/**
 * Generated runs of one to six objectives, as the app reads them: the run to its end as its writer
 * states it (a series per objective with core's decision on each reading), and the run at an earlier
 * position of the time bar as the skeleton projection gives it (the readings so far, with no
 * decision on them, no stated best, no input but the first objective's). The expected figures of
 * every reading are computed here from the generated values alone, by a running best in time order
 * and the plain ratio of a reading to the input, not by any fold or reading of the product.
 */
import type {RunOverview} from '@vibesys/contracts';
import type {Candidate, CandidateTree} from '@vibesys/run-state/candidates';
import fc from 'fast-check';
import type {PerformanceFold, PerformancePoint} from './performance.js';
import type {Direction, RunModel} from './run-model.js';
import {idea} from './timeline-fixtures.js';

const NAMES = ['throughput', 'p99_latency', 'ttft_ms', 'tpot_ms', 'memory_mb', 'cost'] as const;

/** An objective of a generated run. */
export interface ObjectiveSpec {
  readonly name: string;
  readonly direction: 'max' | 'min';
  /** The input's value; the first objective's may be unknown to the run (`inputKnown`). */
  readonly input: number;
}

/** One generated idea: a reading of every objective, and maybe a second one a moment later. */
export interface IdeaSpec {
  readonly passed: boolean;
  /** Its reading of each objective, as a multiple of the objective's input (1: the input's own). */
  readonly ratios: readonly number[];
  /** A second reading of the same idea, by objective; null for none. */
  readonly again: readonly number[] | null;
}

export interface ShapedRun {
  readonly objectives: readonly ObjectiveSpec[];
  /** Whether the run states the first objective's input (the others never have one at a position). */
  readonly inputKnown: boolean;
  readonly ideas: readonly IdeaSpec[];
  /** Where the time bar stands, as a share of the run. */
  readonly cursor: number;
  /** Which of the run's `moments` to read it at besides the cursor. */
  readonly moment: number;
}

/** A reading's multiple of its input: mostly far from 1, sometimes the input itself or within noise of it. */
const ratio = fc.oneof(
  {weight: 6, arbitrary: fc.integer({min: 200, max: 3000}).map(each => each / 1000)},
  {weight: 1, arbitrary: fc.constant(1)},
  {weight: 1, arbitrary: fc.integer({min: -3, max: 3}).map(each => 1 + each / 10000)},
);

const objective = fc.record({
  direction: fc.constantFrom('max' as const, 'min' as const),
  input: fc.integer({min: 10000, max: 100000}),
});

/** Runs of `count` objectives, with ideas that fail, pass without a gain, or set a new best. */
export const shapedRunsOf = (count: number): fc.Arbitrary<ShapedRun> =>
  fc.record({
    objectives: fc
      .array(objective, {minLength: count, maxLength: count})
      .map(each => each.map((spec, at) => ({...spec, name: NAMES[at] ?? `metric_${at}`}))),
    inputKnown: fc.boolean(),
    ideas: fc.array(
      fc.record({
        passed: fc.boolean(),
        ratios: fc.array(ratio, {minLength: count, maxLength: count}),
        again: fc.option(fc.array(ratio, {minLength: count, maxLength: count}), {nil: null}),
      }),
      {minLength: 0, maxLength: 14},
    ),
    cursor: fc.double({min: 0, max: 1, noNaN: true}),
    moment: fc.nat(40),
  });

/** Runs of one to six objectives. */
export const shapedRuns: fc.Arbitrary<ShapedRun> = fc.integer({min: 1, max: 6}).chain(shapedRunsOf);

/** The seconds an idea runs, and the spacing of the ideas: the readings never tie. */
const SPACING = 20;
const TOOK = 5;
const READ_AT = 4;
const AGAIN_AT = 7;

/** The start of idea `at`, in run-clock seconds. */
export function startOf(at: number): number {
  return at * SPACING;
}

/**
 * The run-clock times worth reading a run at, besides its end and its cursor: just after an idea
 * began (running, with no reading yet) and the moment its reading was taken (still running, with
 * one). The run names one of them (`moment`) and the one after it.
 */
export function momentsOf(run: ShapedRun): readonly number[] {
  const all = run.ideas.flatMap((_, at) => [startOf(at) + 1, startOf(at) + READ_AT]);
  if (all.length === 0) return [];
  const from = run.moment % all.length;
  return [all[from] as number, all[(from + 1) % all.length] as number];
}

/** Run-clock time of the run's last event. */
export function endOfRun(run: ShapedRun): number {
  return startOf(run.ideas.length) + SPACING;
}

/** A reading of idea `idea` of objective values `values`, taken at `at`. */
interface Sample {
  readonly idea: number;
  readonly at: number;
  readonly ratios: readonly number[];
}

function samplesOf(run: ShapedRun): readonly Sample[] {
  return run.ideas
    .flatMap((spec, at): Sample[] => [
      {idea: at, at: startOf(at) + READ_AT, ratios: spec.ratios},
      ...(spec.again === null ? [] : [{idea: at, at: startOf(at) + AGAIN_AT, ratios: spec.again}]),
    ])
    .sort((a, b) => a.at - b.at);
}

/** The value of `ratio` of objective `k`'s input. */
function readingValue(run: ShapedRun, k: number, ratioOfInput: number): number {
  return Math.round((run.objectives[k] as ObjectiveSpec).input * ratioOfInput);
}

/** The input of objective `k` as the run states it, or null. */
export function inputOf(run: ShapedRun, k: number): number | null {
  return k === 0 && !run.inputKnown ? null : (run.objectives[k] as ObjectiveSpec).input;
}

/** What the table must say of one idea's reading of one objective. */
export interface ExpectedCell {
  readonly value: number;
  /** The reading over the input where the direction says which way is better; null without an input. */
  readonly multiple: number | null;
  readonly setBest: boolean;
  /** The improvement the idea's steps added in all; null unless it set a step with a known start. */
  readonly gain: number | null;
}

export interface Expected {
  /** The cell of each idea read by then, by the idea's id. */
  readonly cells: ReadonlyMap<string, ExpectedCell>;
  /** The idea that holds the best by then and its value; null when no reading set a step. */
  readonly best: {readonly idea: string; readonly value: number} | null;
  /** Each idea's share of the gain (one objective with a known input); null when there is none to tell. */
  readonly shares: ReadonlyMap<string, number> | null;
}

/** A step of the line: the idea that set it and what it added (null: it began a line with no known input). */
interface Step {
  readonly idea: number;
  readonly value: number;
  readonly gain: number | null;
}

const beats = (direction: ObjectiveSpec['direction'], value: number, than: number): boolean =>
  direction === 'max' ? value > than : value < than;

const improvementOf = (
  direction: ObjectiveSpec['direction'],
  value: number,
  from: number,
): number => (direction === 'max' ? value - from : from - value);

const multipleOf = (direction: ObjectiveSpec['direction'], value: number, input: number): number =>
  direction === 'max' ? value / input : input / value;

/**
 * The steps objective `k`'s line has taken by `until` (null: its end), and the reading each idea
 * stands for: a passed reading that beats the best so far (the input to begin with, when it is
 * known) sets a step, and an idea stands for its last reading that set a step, else its last.
 */
function walk(run: ShapedRun, k: number, until: number | null) {
  const {direction} = run.objectives[k] as ObjectiveSpec;
  const steps: Step[] = [];
  const stands = new Map<number, {value: number; set: boolean}>();
  let standing = inputOf(run, k);
  for (const sample of samplesOf(run).filter(each => until === null || each.at <= until)) {
    const value = readingValue(run, k, sample.ratios[k] as number);
    const passed = (run.ideas[sample.idea] as IdeaSpec).passed;
    const sets = passed && (standing === null || beats(direction, value, standing));
    if (sets) {
      const gain = standing === null ? null : improvementOf(direction, value, standing);
      steps.push({idea: sample.idea, value, gain});
      standing = value;
    }
    const known = stands.get(sample.idea);
    if (known === undefined || sets || !known.set) stands.set(sample.idea, {value, set: sets});
  }
  return {steps, stands};
}

/** The improvement each idea's steps added in all, by the idea's index. */
function gainsByIdea(steps: readonly Step[]): ReadonlyMap<number, number> {
  const gains = new Map<number, number>();
  for (const step of steps) {
    if (step.gain !== null) gains.set(step.idea, (gains.get(step.idea) ?? 0) + step.gain);
  }
  return gains;
}

/**
 * What the run says of objective `k` at run-clock time `until` (null: its end), by definition (see
 * `walk`). A multiple is `value / input`, or `input / value` where lower is better.
 */
export function expectedAt(run: ShapedRun, k: number, until: number | null): Expected {
  const {direction} = run.objectives[k] as ObjectiveSpec;
  const input = inputOf(run, k);
  const {steps, stands} = walk(run, k, until);
  const gains = gainsByIdea(steps);
  const cells = new Map<string, ExpectedCell>();
  for (const [at, held] of stands) {
    cells.set(`i${at}`, {
      value: held.value,
      multiple: input === null ? null : multipleOf(direction, held.value, input),
      setBest: held.set,
      gain: held.set ? (gains.get(at) ?? null) : null,
    });
  }
  const total = [...gains.values()].reduce((sum, gain) => sum + gain, 0);
  const last = steps.at(-1);
  return {
    cells,
    best: last === undefined ? null : {idea: `i${last.idea}`, value: last.value},
    shares:
      run.objectives.length === 1 && input !== null && total > 0
        ? new Map([...gains].map(([at, gain]) => [`i${at}`, gain / total] as const))
        : null,
  };
}

/** Whether objective `k` ever read other than the input (the same band the headline reads as no change). */
export function moved(run: ShapedRun, k: number): boolean {
  const input = inputOf(run, k);
  const {direction} = run.objectives[k] as ObjectiveSpec;
  const values = samplesOf(run).map(sample => readingValue(run, k, sample.ratios[k] as number));
  if (input === null) return new Set(values).size > 1;
  return values.some(value => Math.abs(multipleOf(direction, value, input) - 1) > 0.0005);
}

const OVERVIEW = (run: ShapedRun): RunOverview =>
  ({
    objective: {
      text: '',
      metric: (run.objectives[0] as ObjectiveSpec).name,
      unit: null,
      direction: (run.objectives[0] as ObjectiveSpec).direction,
      objectives: run.objectives.map(spec => ({name: spec.name, direction: spec.direction})),
    },
    benchmarks: {evaluator: null},
  }) as unknown as RunOverview;

/** Idea `at` as the run's writer states it once it ended. */
function candidateOf(run: ShapedRun, at: number): Candidate {
  const spec = run.ideas[at] as IdeaSpec;
  const first = run.objectives[0] as ObjectiveSpec;
  const value = readingValue(run, 0, spec.ratios[0] as number);
  const base = idea(`i${at}`, spec.passed ? 'passed' : 'failed', startOf(at), TOOK);
  const measurement = {
    span: `i${at}-g`,
    at: startOf(at) + READ_AT,
    metricValue: value,
    vsBaseline: value / first.input,
    against: null,
    readings: run.objectives.map((each, k) => ({
      name: each.name,
      value: readingValue(run, k, spec.ratios[k] as number),
      unit: null,
      direction: each.direction,
    })),
    inputReadings: run.objectives.map(each => ({
      name: each.name,
      value: each.input,
      unit: null,
      direction: each.direction,
    })),
    verdict: spec.passed ? 'passed' : 'failed_check',
    failed: null,
  } as const;
  return {...base, measurement, attempts: base.attempts.map(each => ({...each, measurement}))};
}

/** The series of objective `k` as the writer states it: every reading with core's decision on it. */
function seriesOf(run: ShapedRun, k: number): PerformanceFold {
  const spec = run.objectives[k] as ObjectiveSpec;
  const input = inputOf(run, k);
  const points: PerformancePoint[] = [];
  let standing = input;
  samplesOf(run).forEach((sample, seq) => {
    const value = readingValue(run, k, sample.ratios[k] as number);
    const passed = (run.ideas[sample.idea] as IdeaSpec).passed;
    const sets =
      passed &&
      (standing === null || (spec.direction === 'max' ? value > standing : value < standing));
    if (sets) standing = value;
    points.push({
      id: `i${sample.idea}`,
      key: `metric:${spec.name}:${seq}`,
      title: `i${sample.idea}`,
      at: sample.at,
      value,
      result: passed ? 'passed' : 'failed',
      best: sets,
    });
  });
  return {
    points,
    best: points.filter(point => point.best).map(point => ({at: point.at, value: point.value})),
    value: standing ?? undefined,
    input: input ?? undefined,
  };
}

const WHOLE_AT = 1000;

/** The run to its end as its writer states it. */
export function wholeOf(run: ShapedRun): RunModel {
  const candidates = run.ideas.map((_, at) => candidateOf(run, at));
  const tree: CandidateTree = {candidates, best: null, adoptionOpened: false, bestPath: []};
  const first = run.objectives[0] as ObjectiveSpec;
  const firstSeries = seriesOf(run, 0);
  return {
    at: WHOLE_AT,
    candidates: tree,
    direction: first.direction as Direction,
    overview: OVERVIEW(run),
    pauses: [],
    baselines: [],
    inputGates: [],
    changed: new Set<string>(),
    latest: endOfRun(run),
    trace: {spans: new Map(), sequence: WHOLE_AT},
    input: inputOf(run, 0),
    series: new Map(run.objectives.map((spec, k) => [spec.name, seriesOf(run, k)] as const)),
    headline: {
      series: {name: first.name, unit: ''},
      inputValue: inputOf(run, 0) ?? undefined,
      bestValue: firstSeries.value,
      leader: undefined,
      measured: firstSeries.points.length,
    },
  } as unknown as RunModel;
}

/**
 * The run at run-clock time `until` as the skeleton projection reads an earlier position of the time
 * bar: the ideas begun by then, the readings taken by then without core's decision on them (no best
 * flag, `pending`), no stated best and no stated input on a series, no headline, and the first
 * objective's input only.
 */
export function viewedAt(run: ShapedRun, whole: RunModel, until: number): RunModel {
  const candidates = whole.candidates.candidates
    .filter(each => each.startedAt <= until)
    .map((each, at) =>
      startOf(at) + TOOK <= until ? each : idea(each.id, 'running', startOf(at), TOOK),
    )
    .map(each => (each.outcome === 'running' ? {...each, measurement: null} : each));
  return {
    ...whole,
    at: until + 1,
    candidates: {candidates, best: null, adoptionOpened: false, bestPath: []},
    latest: until,
    headline: undefined,
    series: new Map(
      run.objectives.map(spec => {
        const states = whole.series?.get(spec.name) as PerformanceFold;
        return [
          spec.name,
          {
            points: states.points
              .filter(point => point.at <= until)
              .map(point => ({...point, result: 'pending' as const, best: false})),
            best: [],
            refusal: 'Performance facts unavailable',
          },
        ] as const;
      }),
    ),
  } as unknown as RunModel;
}

/** The position of the time bar the run's cursor names, in run-clock seconds. */
export function scrubOf(run: ShapedRun): number {
  return Math.floor(run.cursor * endOfRun(run));
}
