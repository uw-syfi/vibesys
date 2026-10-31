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
}

export interface Run {
  readonly direction: 'max' | 'min';
  readonly input: number;
  /** The input as each session measured it again. */
  readonly sessionInputs: readonly number[];
  readonly ideas: readonly Generated[];
  /** Where the time bar stands, as a share of the run. */
  readonly cursor: number;
}

export const run: fc.Arbitrary<Run> = fc
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
      }),
      {minLength: 0, maxLength: 16},
    ),
    cursor: fc.double({min: 0, max: 1, noNaN: true}),
  })
  .map(each => ({
    ...each,
    // The first session's re-measure is the run's input.
    sessionInputs: [each.input, ...each.sessionInputs.slice(1)],
  }));

const NAME = 'metric';

/** The idea of `item`, measured: its own session's input is what its `vs input` is over. */
export function candidateOf(item: Generated, at: number, run: Run): Candidate {
  const input = run.sessionInputs[item.session % run.sessionInputs.length] ?? run.input;
  const base = idea(`i${at}`, item.passed ? 'passed' : 'failed', item.at, 5);
  const measurement = {
    span: `i${at}-g`,
    at: item.at + 5,
    metricValue: item.value,
    vsBaseline: item.value / input,
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

/** The run as its writer states it: a series with the best and the input, and the headline of both. */
export function readFromWriter(
  run: Run,
  candidates: readonly Candidate[],
  until: number,
): RunModel {
  const events = readFromEvents(run, candidates);
  const fold: PerformanceFold = performanceFold(candidates, run.direction, run.input, NAME);
  const leader = fold.points.filter(point => point.best).at(-1);
  const stated = {...fold, value: leader?.value ?? run.input, input: run.input};
  return {
    ...events,
    series: new Map([[NAME, stated]]),
    headline: {
      series: {name: NAME, unit: ''},
      inputValue: run.input,
      bestValue: leader?.value,
      leader: undefined,
      measured: fold.points.length,
    },
    // The writer's leader is the one the Ideas list badges as the best.
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
