import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {Span} from '@vibesys/contracts';
import {CandidateFold} from '@vibesys/run-state/candidates';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {inputValueOf, performanceFold} from './performance.js';

/**
 * A run's tile (the host helper's digest `points`, `server.run_digest`) must draw what the run
 * screen's performance chart draws. Both read the scenarios under
 * `tests/conformance/fixtures/run-dashboard`: an idea's outcome, what its gate read against an input
 * of 100, and when it landed. The page's fold is held to the corpus here and, over generated
 * scenarios, to the plain statement `chartOf` that the corpus pins; the helper's side does the same
 * in `tests/server/test_run_dashboard.py`.
 */

const INPUT = 100;

type Outcome = 'passed' | 'failed' | 'running';
type Verdict = 'kept' | 'not_better' | 'failed' | 'running';
type Direction = 'max' | 'min';

interface Idea {
  readonly outcome: Outcome;
  readonly reading: number | null;
  readonly landed: number;
}
interface ChartPoint {
  readonly ratio: number | null;
  readonly verdict: Verdict;
}
interface Scenario {
  readonly name: string;
  readonly direction: Direction;
  readonly ideas: readonly Idea[];
  readonly points: readonly ChartPoint[];
}

const CORPUS = resolve(
  import.meta.dir,
  '../../../../../tests/conformance/fixtures/run-dashboard/scenarios.json',
);
const scenarios = (JSON.parse(readFileSync(CORPUS, 'utf8')) as {scenarios: Scenario[]}).scenarios;

function span(
  id: string,
  parent: string | null,
  kind: 'workstream' | 'gate',
  fields: {
    readonly status: Span['status'];
    readonly started: number;
    readonly ended: number | null;
    readonly verdict: string | null;
    readonly attrs: Record<string, unknown>;
  },
): Span {
  return {
    id,
    parent,
    type: kind === 'gate' ? 'operation' : 'flow',
    name: kind,
    status: fields.status,
    waiting_on: [],
    attempts: [],
    started_at: fields.started,
    ended_at: fields.ended,
    failure: null,
    stop: null,
    invocation: null,
    label: {
      kind,
      title: id,
      links: [],
      attrs_json: JSON.stringify(fields.attrs),
      verdict: fields.verdict,
      reason: null,
    },
  };
}

/** The spans of a run that played `ideas`: one workstream and, with a reading, one gate each. */
function spansOf(ideas: readonly Idea[]): Span[] {
  return ideas.flatMap((idea, at): Span[] => {
    const ws = `flow:${at}`;
    const ended = idea.outcome === 'running' ? null : idea.landed + 1;
    const workstream = span(ws, null, 'workstream', {
      status:
        idea.outcome === 'running' ? 'running' : idea.outcome === 'passed' ? 'done' : 'failed',
      started: 10 * at,
      ended,
      verdict: idea.outcome === 'running' ? null : idea.outcome,
      attrs: {hypothesis_id: `h${at}`, probe: false},
    });
    if (idea.reading === null) return [workstream];
    const gate = span(`op:${at}`, ws, 'gate', {
      status: 'done',
      started: idea.landed - 0.5,
      ended: idea.landed,
      verdict: idea.outcome === 'passed' ? 'passed' : 'failed_check',
      attrs: {metric_value: idea.reading, vs_baseline: idea.reading / INPUT},
    });
    return [workstream, gate];
  });
}

/** What the run screen's fold says of `ideas`, for the ideas whose gate read a value. */
function pageChart(ideas: readonly Idea[], direction: Direction): ChartPoint[] {
  const fold = new CandidateFold();
  fold.applySpans(spansOf(ideas));
  const {candidates} = fold.tree();
  const chart = performanceFold(candidates, direction, inputValueOf(candidates));
  return chart.points.map(point => ({
    ratio: direction === 'min' ? INPUT / point.value : point.value / INPUT,
    verdict:
      point.result === 'pending'
        ? 'running'
        : point.result === 'failed'
          ? 'failed'
          : point.best
            ? 'kept'
            : 'not_better',
  }));
}

/**
 * The chart, stated plainly (the statement `tests/server/test_run_dashboard.py` holds the digest
 * to): ideas are plotted in the order their readings landed (ties in the order they began), the
 * input stands until a passed idea beats it, and that idea is kept; a passed idea that does not is
 * not better; failing and running are the workstream's own outcome.
 */
function chartOf(ideas: readonly Idea[], direction: Direction): ChartPoint[] {
  const landed = (at: number): number => {
    const idea = ideas[at] as Idea;
    if (idea.reading !== null) return idea.landed;
    return idea.outcome === 'running' ? 10 * at : idea.landed + 1;
  };
  const order = ideas.map((_, at) => at).sort((a, b) => landed(a) - landed(b) || a - b);
  let standing: number | null = ideas.some(idea => idea.reading !== null) ? INPUT : null;
  return order.map((at): ChartPoint => {
    const idea = ideas[at] as Idea;
    const {reading} = idea;
    const ratio = reading === null ? null : direction === 'min' ? INPUT / reading : reading / INPUT;
    if (idea.outcome !== 'passed') return {ratio, verdict: idea.outcome};
    const beats =
      reading !== null &&
      (standing === null || (direction === 'min' ? reading < standing : reading > standing));
    if (beats) standing = reading;
    return {ratio, verdict: beats ? 'kept' : 'not_better'};
  });
}

const measured = (points: readonly ChartPoint[]): ChartPoint[] =>
  points.filter(point => point.ratio !== null);

function expectSame(got: readonly ChartPoint[], wanted: readonly ChartPoint[]): void {
  expect(got.map(point => point.verdict)).toEqual(wanted.map(point => point.verdict));
  got.forEach((point, at) => {
    const expected = wanted[at]?.ratio ?? null;
    if (expected === null) expect(point.ratio).toBeNull();
    else expect(point.ratio).toBeCloseTo(expected, 9);
  });
}

describe('the run page draws the ideas of a run as its tile does', () => {
  for (const scenario of scenarios) {
    test(`${scenario.name}: the page's chart is the corpus's, for the ideas that were measured`, () => {
      expectSame(pageChart(scenario.ideas, scenario.direction), measured(scenario.points));
    });

    test(`${scenario.name}: the plain statement is the corpus`, () => {
      expectSame(chartOf(scenario.ideas, scenario.direction), scenario.points);
    });
  }

  test('over generated runs the page agrees with the statement for every measured idea', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const ideas = Array.from(
        {length: random.int(13)},
        (): Idea => ({
          outcome: random.pick(['passed', 'passed', 'failed', 'running'] as const),
          reading: random.int(4) === 0 ? null : 10 + random.int(291),
          landed: random.int(401),
        }),
      );
      const direction = random.pick(['max', 'min'] as const);

      expectSame(pageChart(ideas, direction), measured(chartOf(ideas, direction)));
    }
  });

  test('the corpus holds scenarios', () => {
    expect(scenarios.length).toBeGreaterThan(0);
  });
});
