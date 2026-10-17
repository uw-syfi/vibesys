import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {Seeded} from '@vibesys/store/testing';
import {ideaTree} from './ideas-tree.js';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {EMPTY_TRACE_LOG, type TraceLog, traceStep} from './live.js';
import {createRunReader} from './run-model.js';
import {scrubberView} from './scrub.js';

/**
 * The Run screen at the scale of weeks of running (design v3.4): a seeded generator of a run's
 * span events, and the work the screen's state does on it, counted (never timed):
 *
 * 1. folding N events costs work linear in N;
 * 2. one new event on a long run costs work independent of the run's length;
 * 5. the ideas, the tree and the agents are not rebuilt while the run is unchanged;
 * 7. the same seed gives the same run, byte for byte.
 */

const SEED = 20261016;
const DAY = 86_400;
const ROOT = 'flow:search/#1';
const ENDED = new Set(['done', 'failed', 'stopped']);

interface ScaleShape {
  readonly seed: number;
  readonly ideas: number;
  /** Agent turns per idea (each a started and an ended version). */
  readonly turns: number;
  /** Progress versions of each turn while it runs: more events, the same spans. */
  readonly updates: number;
}

const WEEKS: ScaleShape = {seed: SEED, ideas: 600, turns: 1, updates: 0};

type Outcome = 'passed' | 'failed' | 'running';

interface Idea {
  readonly n: number;
  readonly outcome: Outcome;
  readonly on: number | null;
  readonly vs: number;
  readonly start: number;
  readonly onPath: boolean;
}

function outcomeOf(random: Seeded, shape: ScaleShape, n: number, onPath: boolean): Outcome {
  if (n >= shape.ideas - 3) return 'running';
  return onPath || random.int(shape.ideas) < 25 ? 'passed' : 'failed';
}

/** What an idea builds on: the path's last idea for the next on the path, else a recent passed. */
function parentOf(random: Seeded, passed: readonly Idea[], onPath: boolean): number | null {
  if (passed.length === 0) return null;
  const back = onPath ? 0 : random.int(Math.min(3, passed.length));
  return passed.at(-1 - back)?.n ?? null;
}

/** 600 ideas over 14 days: a 15-long winning path to 7.35×, about 40 passed, 3 running. */
function ideasOf(random: Seeded, shape: ScaleShape): Idea[] {
  const span = (14 * DAY) / shape.ideas;
  const pathAt = new Set(Array.from({length: 15}, (_, k) => Math.floor((k * shape.ideas) / 16)));
  const ideas: Idea[] = [];
  const passed: Idea[] = [];
  let best = 1;
  for (let n = 0; n < shape.ideas; n += 1) {
    const onPath = pathAt.has(n);
    const on = parentOf(random, passed, onPath);
    const outcome = outcomeOf(random, shape, n, onPath);
    const vs = onPath ? best + (7.35 - 1) / 15 : best * (0.6 + random.int(35) / 100);
    if (onPath) best = vs;
    const idea: Idea = {n, outcome, on, vs, start: Math.floor(n * span), onPath};
    if (outcome === 'passed') passed.push(idea);
    ideas.push(idea);
  }
  return ideas;
}

function flow(idea: Idea, status: Span['status']): Span {
  const links = idea.on === null ? [] : [{rel: 'builds_on', to: `flow:w${idea.on}/#1`}];
  return {
    id: `flow:w${idea.n}/#1`,
    parent: ROOT,
    type: 'flow',
    name: 'workstream',
    status,
    started_at: idea.start,
    ended_at: ENDED.has(status) ? idea.start + 1_800 : null,
    label: {
      kind: 'workstream',
      title: `idea ${idea.n}`,
      ...(status === 'done' ? {verdict: idea.outcome === 'passed' ? 'passed' : 'failed'} : {}),
      links,
      attrs_json: JSON.stringify({
        hypothesis_id: `h${idea.n}`,
        idea: `try idea ${idea.n}`,
        ...(status === 'done' && idea.outcome === 'passed' ? {revision: `c${idea.n}`} : {}),
      }),
    },
  } as Span;
}

/** 58 agents: one orchestrator, six planners, the rest workers; judges started by two planners. */
function agentOf(random: Seeded): {agent: string; role: string; started_by: string[]} {
  const worker = random.int(51);
  const planner = `planner-${worker % 6}`;
  if (worker % 10 === 0) {
    return {
      agent: `judge-${worker}`,
      role: 'judge',
      started_by: [planner, `planner-${(worker + 1) % 6}`],
    };
  }
  return {agent: `worker-${worker}`, role: 'implementer', started_by: [planner]};
}

function turn(random: Seeded, idea: Idea, k: number, status: Span['status']): Span {
  const start = idea.start + 1 + k * 10;
  return {
    id: `flow:w${idea.n}/turn/#${k + 1}`,
    parent: `flow:w${idea.n}/#1`,
    type: 'operation',
    name: 'turn',
    status,
    started_at: start,
    ended_at: ENDED.has(status) ? start + 9 : null,
    label: {kind: 'agent_turn', title: 'implementer', attrs_json: JSON.stringify(agentOf(random))},
  };
}

function gate(idea: Idea): Span {
  return {
    id: `flow:w${idea.n}/gate/#1`,
    parent: `flow:w${idea.n}/#1`,
    type: 'operation',
    name: 'measure',
    status: 'done',
    started_at: idea.start + 1_000,
    ended_at: idea.start + 1_200,
    label: {
      kind: 'gate',
      title: 'benchmark',
      verdict: idea.outcome === 'passed' ? 'passed' : 'failed_check',
      attrs_json: JSON.stringify({
        stage: 'benchmark',
        metric_value: 100 * idea.vs,
        vs_baseline: idea.vs,
      }),
    },
  };
}

/** One idea's span versions, in the order the run writes them. */
function versionsOf(idea: Idea, shape: ScaleShape): Span[] {
  const versions: Span[] = [flow(idea, 'running')];
  for (let k = 0; k < shape.turns; k += 1) {
    // Each turn draws its agent from its own seed, so its versions name the same agent.
    const agentOfTurn = (status: Span['status']): Span =>
      turn(new Seeded(shape.seed + idea.n * 1_000 + k), idea, k, status);
    for (let update = 0; update <= shape.updates; update += 1)
      versions.push(agentOfTurn('running'));
    if (idea.outcome !== 'running' || k < shape.turns - 1) versions.push(agentOfTurn('done'));
  }
  if (idea.outcome === 'running') return versions;
  versions.push(gate(idea), flow(idea, 'done'));
  return versions;
}

/** The generated run's span events, oldest first. */
function scaleRun(shape: ScaleShape): RunEvent[] {
  const random = new Seeded(shape.seed);
  const root: Span = {
    id: ROOT,
    parent: null,
    type: 'flow',
    name: 'search',
    status: 'running',
    started_at: 0,
  };
  const ideas = ideasOf(random, shape);
  const best = ideas.filter(idea => idea.onPath && idea.outcome === 'passed').at(-1);
  const adoption: Span = {
    id: 'op:adopt',
    parent: ROOT,
    type: 'operation',
    name: 'adopt',
    status: 'running',
    started_at: 0,
    label: {
      kind: 'adoption',
      attrs_json: JSON.stringify({candidate: best === undefined ? null : `c${best.n}`}),
    },
  };
  const versions = [root, adoption, ...ideas.flatMap(idea => versionsOf(idea, shape))];
  const seen = new Set<string>();
  return versions.map((span, index) => {
    const type = !seen.has(span.id)
      ? 'span_started'
      : ENDED.has(span.status)
        ? 'span_ended'
        : 'span_updated';
    seen.add(span.id);
    return {
      protocol_version: 1,
      run_id: 'weeks',
      sequence: index + 1,
      timestamp: '2026-10-16T00:00:00Z',
      type,
      data: {kind: type, span},
    } as RunEvent;
  });
}

function logOf(events: readonly RunEvent[]): TraceLog {
  return traceStep(EMPTY_TRACE_LOG, {kind: 'events', events});
}

/** The work of folding `events` into the screen's state: the trace, then the run model. */
function foldWork(events: readonly RunEvent[]): number {
  let units = 0;
  const log = traceStep(EMPTY_TRACE_LOG, {kind: 'events', events}, count => (units += count));
  createRunReader({count: count => (units += count)}).read(log, null);
  return units;
}

describe('the Run screen at scale', () => {
  test('the generated run is the shape the design names, and the same for the same seed', () => {
    const run = scaleRun(WEEKS);
    expect(JSON.stringify(scaleRun(WEEKS))).toBe(JSON.stringify(run));
    expect(JSON.stringify(scaleRun({...WEEKS, seed: SEED + 1}))).not.toBe(JSON.stringify(run));
    const model = createRunReader().read(logOf(run), null);
    const counts = ideasView(model, IDEAS_FILTER_DEFAULT).counts;
    expect(counts.running).toBe(3);
    expect(counts.passed + counts.failed + counts.running).toBe(600);
    expect(counts.passed).toBeGreaterThanOrEqual(15);
    expect(model.candidates.bestPath.length).toBeGreaterThanOrEqual(15);
    expect(model.agents.agents.length).toBeGreaterThan(20);
  });

  test('folding a run costs work linear in its events', () => {
    const small = scaleRun({...WEEKS, turns: 2});
    const large = scaleRun({...WEEKS, turns: 20});
    const ratio = foldWork(large) / large.length / (foldWork(small) / small.length);
    expect(ratio).toBeGreaterThan(0.5);
    expect(ratio).toBeLessThan(2);
  });

  test('one new event on a long run costs the same work as on a short one', () => {
    const work = (updates: number): number => {
      const run = scaleRun({...WEEKS, turns: 3, updates});
      let units = 0;
      const reader = createRunReader({count: count => (units += count)});
      const log = logOf(run.slice(0, -1));
      reader.read(log, null);
      units = 0;
      const next = traceStep(log, {kind: 'events', events: run.slice(-1)}, count => {
        units += count;
      });
      reader.read(next, null);
      scrubberView(next, {at: 'live'}, false);
      return units;
    };
    const short = work(1);
    expect(short).toBeGreaterThan(0);
    expect(work(30)).toBe(short);
  });

  test('the ideas, the tree and the agents are not rebuilt while the run is unchanged', () => {
    const log = logOf(scaleRun(WEEKS));
    const reader = createRunReader();
    const model = reader.read(log, null);
    let units = 0;
    const meter = {count: (count: number) => (units += count)};
    const ideas = ideasView(model, IDEAS_FILTER_DEFAULT, meter);
    const shape = {filter: IDEAS_FILTER_DEFAULT, open: new Set<string>(), pinned: null};
    const tree = ideaTree(model, shape);
    const built = units;
    expect(built).toBeGreaterThan(0);
    const again = reader.read(log, null);
    expect(again).toBe(model);
    expect(again.agents).toBe(model.agents);
    expect(ideasView(again, {...IDEAS_FILTER_DEFAULT}, meter)).toBe(ideas);
    expect(ideaTree(again, shape).rows).toEqual(tree.rows);
    expect(units).toBe(built);
  });
});
