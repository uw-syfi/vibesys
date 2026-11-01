/**
 * Generated runs for the tests of the one reading of an objective's best: the way real runs disagree
 * with themselves (the input measured again in every session, ideas that fail or pass without beating
 * the best, measurements out of order), read as the writer states them and as events say them.
 */
import type {RunOverview} from '@vibesys/contracts';
import type {Candidate, CandidateTree} from '@vibesys/run-state/candidates';
import fc from 'fast-check';
import {heroView} from './headline.js';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {withLiveBest} from './live-best.js';
import {objectiveCharts} from './objective-chart.js';
import {readObjectives} from './objective-reading.js';
import {type PerformanceFold, performanceFold} from './performance.js';
import type {Direction, RunModel} from './run-model.js';
import {idea} from './timeline-fixtures.js';

/** One generated idea: what it measured, when, in which session, and whether it passed. */
export interface Generated {
  readonly value: number;
  readonly at: number;
  readonly passed: boolean;
  /** Which session measured it; each session measured the input again, to its own value. */
  readonly session: number;
  /** Whether its measurement states its own ratio to the input (a resumed session may not). */
  readonly compared: boolean;
  /** Whether the writer keeps an earlier sample of it, as it does of an idea benchmarked twice. */
  readonly sampled: boolean;
}

export interface Run {
  readonly direction: 'max' | 'min';
  readonly input: number;
  /** The input as each session measured it again. */
  readonly sessionInputs: readonly number[];
  readonly ideas: readonly Generated[];
  /** Where the time bar stands, as a share of the run. */
  readonly cursor: number;
  /**
   * The best the writer states for the run's headline and its series, whatever its points say; null:
   * it states the best of the run's readings (`oracleBest`). Not read from events.
   */
  readonly stated: number | null;
}

/** Runs as generated: their ideas, sessions, the writer's statement and where the time bar stands. */
export const generatedRuns: fc.Arbitrary<Run> = fc
  .record({
    direction: fc.constantFrom('max', 'min'),
    input: fc.integer({min: 10, max: 5000}),
    sessionInputs: fc.array(fc.integer({min: 10, max: 5000}), {minLength: 1, maxLength: 3}),
    ideas: fc.array(
      fc.record({
        value: fc.integer({min: 1, max: 10000}),
        at: fc.integer({min: 1, max: 600}),
        passed: fc.boolean(),
        session: fc.nat(2),
        compared: fc.boolean(),
        sampled: fc.boolean(),
      }),
      {minLength: 0, maxLength: 16},
    ),
    cursor: fc.double({min: 0, max: 1, noNaN: true}),
    // A writer states its best as it likes: the best of the readings (null), or any value.
    stated: fc.option(fc.oneof(fc.integer({min: 1, max: 12000}), fc.constant('input' as const)), {
      nil: null,
    }),
  })
  .map(each => ({
    ...each,
    // The first session's re-measure is the run's input.
    sessionInputs: [each.input, ...each.sessionInputs.slice(1)],
    // A writer may state the input itself as its best: nothing beat it.
    stated: each.stated === 'input' ? each.input : each.stated,
    ideas: inTimeOrder(each.ideas),
  }));

/**
 * The ideas as a run has them: in the order they were measured, the ideas of a resumed session
 * after those of every session before it, and the first idea of the run, which ran after the input
 * gate, from the first session and stating its ratio to the input (a session resumed later may
 * measure its ideas without one). An idea of a later session measured before the first idea of the
 * run is not a run: its session's input would be the only input a time bar position before the
 * rest could read.
 */
function inTimeOrder(ideas: readonly Generated[]): Generated[] {
  let session = 0;
  return [...ideas]
    .sort((a, b) => a.at - b.at)
    .map((idea, index) => {
      session = index === 0 ? 0 : Math.max(session, idea.session);
      return {...idea, session, compared: index === 0 ? true : idea.compared};
    });
}

const NAME = 'metric';

/**
 * The best of `run` by its definition alone, from the generated ideas and not from any fold of them:
 * the best passed value that beats `input`, where the direction says which way is better; null when
 * none does, so the input stands.
 */
export function oracleBest(run: Run, input: number): number | null {
  const beating = run.ideas
    .filter(
      item => item.passed && (run.direction === 'max' ? item.value > input : item.value < input),
    )
    .map(item => item.value);
  if (beating.length === 0) return null;
  return run.direction === 'max' ? Math.max(...beating) : Math.min(...beating);
}

/** The idea of `item`, measured: its own session's input is what its `vs input` is over. */
export function candidateOf(item: Generated, at: number, run: Run): Candidate {
  const input = run.sessionInputs[item.session % run.sessionInputs.length] ?? run.input;
  const base = idea(`i${at}`, item.passed ? 'passed' : 'failed', item.at, 5);
  const measurement = {
    span: `i${at}-g`,
    at: item.at + 5,
    metricValue: item.value,
    vsBaseline: item.compared ? item.value / input : null,
    against: null,
    readings: [{name: NAME, value: item.value, unit: null, direction: run.direction}],
    inputReadings: [{name: NAME, value: input, unit: null, direction: run.direction}],
    verdict: item.passed ? 'passed' : 'failed_check',
    failed: null,
  } as const;
  return {...base, measurement, attempts: base.attempts.map(each => ({...each, measurement}))};
}

const OVERVIEW = (direction: Direction): RunOverview =>
  ({
    objective: {
      text: '',
      metric: NAME,
      unit: null,
      direction,
      objectives: [{name: NAME, direction: direction ?? 'max'}],
    },
    benchmarks: {evaluator: null},
  }) as unknown as RunOverview;

/** The run as events say it: ideas, an overview, and the best so far named while it works. */
export function readFromEvents(run: Run, candidates: readonly Candidate[]): RunModel {
  const tree: CandidateTree = {candidates, best: null, adoptionOpened: false, bestPath: []};
  return {
    candidates: withLiveBest(tree, run.direction),
    direction: run.direction,
    overview: OVERVIEW(run.direction),
    pauses: [],
    baselines: [],
    inputGates: [],
    changed: new Set<string>(),
    latest: Math.max(0, ...candidates.map(each => each.measurement?.at ?? 0)),
  } as unknown as RunModel;
}

/** `points` with the earlier sample a sampled idea has: its value read before the gate read it. */
function withEarlySamples(run: Run, points: PerformanceFold['points']): PerformanceFold['points'] {
  const sampled = new Set(run.ideas.flatMap((item, at) => (item.sampled ? [`i${at}`] : [])));
  return points
    .flatMap(point =>
      sampled.has(point.id)
        ? [{...point, key: `${point.id}:early`, at: point.at - 3, best: false}, point]
        : [point],
    )
    .sort((a, b) => a.at - b.at);
}

/**
 * The run as its writer states it: a series with the best and the input, and the headline of both. The
 * best it states is the run's own statement (`Run.stated`), else the best of the generated readings;
 * its points' decisions come from the fold, so the three can differ, as a writer's can.
 */
export function readFromWriter(
  run: Run,
  candidates: readonly Candidate[],
  until: number,
): RunModel {
  const events = readFromEvents(run, candidates);
  const folded = performanceFold(candidates, run.direction, run.input, NAME);
  const fold: PerformanceFold = {...folded, points: withEarlySamples(run, folded.points)};
  const best = run.stated ?? oracleBest(run, run.input);
  const stated = {...fold, value: best ?? run.input, input: run.input};
  // The writer's leader is the idea that has the stated best, the one that set it first.
  const leader =
    best === null
      ? undefined
      : (fold.points.find(point => point.best && point.value === best) ??
        fold.points.find(point => point.result === 'passed' && point.value === best));
  return {
    ...events,
    series: new Map([[NAME, stated]]),
    headline: {
      series: {name: NAME, unit: ''},
      inputValue: run.input,
      bestValue: best ?? undefined,
      leader: undefined,
      measured: fold.points.length,
    },
    // The Ideas list badges the writer's leader as the best.
    candidates: {
      ...events.candidates,
      best: leader === undefined ? null : {candidate: leader.id, adopted: false},
      bestPath: leader === undefined ? [] : [leader.id],
    },
    latest: until,
  } as unknown as RunModel;
}

/**
 * What the viewed model at `until` holds: the ideas whose reading was taken by then (the time the
 * run's own fold gives it). A run read from events names the best of them as the run does while it
 * works; the writer states its leader for the whole run only, so a viewed model names none.
 */
export function viewedAt(
  whole: RunModel,
  run: Run,
  until: number,
  reader: 'events' | 'the writer',
): RunModel {
  const [reading] = readObjectives(whole);
  const taken = new Set(
    (reading?.fold.points ?? []).filter(point => point.at <= until).map(point => point.id),
  );
  const candidates = whole.candidates.candidates.filter(each => taken.has(each.id));
  const tree: CandidateTree = {candidates, best: null, adoptionOpened: false, bestPath: []};
  const named = reader === 'events' ? withLiveBest(tree, run.direction) : tree;
  return {...whole, candidates: named, latest: until};
}

/** The row of idea `id` in the Ideas list of `model`; null when the list has none. */
export function rowOf(
  model: RunModel,
  id: string | null,
): {readonly idea: string; readonly figure: string} | null {
  const row = ideasView(model, IDEAS_FILTER_DEFAULT).rows.find(each => each.id === id);
  return row === undefined ? null : {idea: row.id, figure: row.figure};
}

/** The multiple the Ideas list gives its best row, and the idea it is the row of; null with no best. */
export function bestRow(model: RunModel): {readonly idea: string; readonly figure: string} | null {
  const row = ideasView(model, IDEAS_FILTER_DEFAULT).rows.find(each =>
    each.badges.some(badge => badge.label === 'best' || badge.label === 'adopted'),
  );
  return row === undefined ? null : {idea: row.id, figure: row.figure};
}

/** What the chart says of its best at `scrub` (null: the end): its headline and the end of its line. */
export function chartBest(whole: RunModel, scrub: number | null) {
  const [chart] = objectiveCharts(whole, {start: 0, now: whole.latest, scrub});
  const [headline] = heroView(whole, undefined, scrub).headlines;
  const input = chart?.steps[0]?.y;
  // The line to the cursor: its last step at or before it, which is the best so far.
  const reached = chart?.steps.filter(step => scrub === null || step.x * 60 <= scrub).at(-1);
  return {chart, headline, input, reached};
}

/** How a run is read, as the events say it and as its writer states it, over the same ideas. */
export const READERS = [
  ['events', (each: Run, candidates: readonly Candidate[]) => readFromEvents(each, candidates)],
  [
    'the writer',
    (each: Run, candidates: readonly Candidate[]) =>
      readFromWriter(
        each,
        candidates,
        Math.max(0, ...candidates.map(item => item.measurement?.at ?? 0)),
      ),
  ],
] as const;
