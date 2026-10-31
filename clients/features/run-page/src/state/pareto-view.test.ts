import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {heroView} from './headline.js';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';
import {paretoView} from './pareto-view.js';
import {createRunReader} from './run-model.js';

const ROOT: Span = {
  id: 'flow:search/#1',
  parent: null,
  type: 'flow',
  name: 'search',
  status: 'running',
  started_at: 0,
};

const ENDED = new Set(['done', 'failed', 'stopped']);

const AXES = [
  {name: 'total_ops_per_sec', direction: 'max'},
  {name: 'p99_latency', direction: 'min'},
] as const;

const row = (name: string, value: number) => ({
  name,
  value,
  unit: name === 'p99_latency' ? 'ms' : 'ops/s',
  direction: name === 'p99_latency' ? 'min' : 'max',
});
const both = (throughput: number, latency: number) => [
  row('total_ops_per_sec', throughput),
  row('p99_latency', latency),
];
const INPUT = both(1000, 20);

/** Minutes from the run's start when idea `n`'s gate ended. */
const at = (n: number): number => (n * 600 + 500) / 60;

interface Idea {
  readonly n: number;
  readonly verdict: 'passed' | 'failed';
  readonly throughput: number;
  readonly latency: number;
}

function workstream(idea: Idea, status: Span['status']): Span {
  return {
    id: `flow:w${idea.n}/#1`,
    parent: ROOT.id,
    type: 'flow',
    name: 'workstream',
    status,
    started_at: idea.n * 600,
    ended_at: ENDED.has(status) ? idea.n * 600 + 540 : null,
    label: {
      kind: 'workstream',
      title: `idea ${idea.n}`,
      ...(status === 'done' ? {verdict: idea.verdict} : {}),
      links: [],
      attrs_json: JSON.stringify({
        hypothesis_id: `h${idea.n}`,
        ...(status === 'done' && idea.verdict === 'passed' ? {revision: `c${idea.n}`} : {}),
      }),
    },
  } as Span;
}

function gate(idea: Idea): Span {
  return {
    id: `flow:w${idea.n}/gate/#1`,
    parent: `flow:w${idea.n}/#1`,
    type: 'operation',
    name: 'measure',
    status: 'done',
    started_at: idea.n * 600 + 400,
    ended_at: idea.n * 600 + 500,
    label: {
      kind: 'gate',
      title: 'benchmark',
      verdict: idea.verdict === 'passed' ? 'passed' : 'slower',
      attrs_json: JSON.stringify({
        stage: 'benchmark',
        metric_value: idea.throughput,
        vs_baseline: idea.throughput / 1000,
        metrics: both(idea.throughput, idea.latency),
        input_metrics: INPUT,
      }),
    },
  };
}

const IDEAS: readonly Idea[] = [
  {n: 1, verdict: 'passed', throughput: 1500, latency: 18},
  {n: 2, verdict: 'passed', throughput: 2500, latency: 30},
  {n: 3, verdict: 'failed', throughput: 900, latency: 25},
  {n: 4, verdict: 'passed', throughput: 1200, latency: 10},
];

function eventsOf(overview: unknown, ideas: readonly Idea[]): RunEvent[] {
  const spans = [
    ROOT,
    ...ideas.flatMap(idea => [workstream(idea, 'running'), gate(idea), workstream(idea, 'done')]),
  ];
  const seen = new Set<string>();
  const started = {
    protocol_version: 1,
    run_id: 'run',
    sequence: 1,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'run_started',
    data: {kind: 'run_started', outer_loop: 'iterate', input: 'x', overview},
  } as unknown as RunEvent;
  return [
    started,
    ...spans.map((span, index) => {
      const type = !seen.has(span.id)
        ? 'span_started'
        : ENDED.has(span.status)
          ? 'span_ended'
          : 'span_updated';
      seen.add(span.id);
      return {
        protocol_version: 1,
        run_id: 'run',
        sequence: index + 2,
        timestamp: '2026-01-01T00:00:00Z',
        type,
        data: {kind: type, span},
      } as RunEvent;
    }),
  ];
}

function modelOf(objectives: readonly {name: string; direction: 'max' | 'min'}[], at = null) {
  const overview = {
    overview_version: 1,
    objective: {text: 'Go faster.', metric: 'total_ops_per_sec', direction: 'max', objectives},
    benchmarks: {},
  };
  const log = traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: eventsOf(overview, IDEAS)});
  return createRunReader().read(log, at);
}

describe('the trade-off chart of a run with two objectives', () => {
  test('has the input and a point per settled idea, in minutes from the run start, with words and units', () => {
    const view = paretoView(modelOf(AXES), 0, null);

    expect(view?.x).toEqual({name: 'Total ops/sec', unit: 'ops/s', direction: 'higher'});
    expect(view?.y).toEqual({name: 'P99 latency', unit: 'ms', direction: 'lower'});
    expect(view?.cursor).toBeUndefined();
    expect(view?.points.map(p => [p.id, p.label, p.x, p.y, p.time, p.state])).toEqual([
      ['input', 'Input', 1000, 20, 0, 'input'],
      ['flow:w1/#1', 'idea 1', 1500, 18, at(1), 'passed'],
      ['flow:w2/#1', 'idea 2', 2500, 30, at(2), 'passed'],
      ['flow:w3/#1', 'idea 3', 900, 25, at(3), 'failed'],
      ['flow:w4/#1', 'idea 4', 1200, 10, at(4), 'passed'],
    ]);
  });

  test('puts the cursor at the scrubbed time, and rings the open idea and the lit chain once each', () => {
    const view = paretoView(
      modelOf(AXES),
      0,
      1200,
      'flow:w2/#1',
      new Set(['flow:w1/#1', 'flow:w2/#1']),
    );

    expect(view?.cursor).toBe(20);
    expect(view?.highlighted).toEqual(['flow:w1/#1', 'flow:w2/#1']);
    expect(paretoView(modelOf(AXES), 0, null, 'flow:w4/#1')?.highlighted).toEqual(['flow:w4/#1']);
  });

  test('is drawn from the whole run: a model scrubbed to an earlier sequence changes nothing but the cursor', () => {
    const whole = paretoView(modelOf(AXES), 0, null);
    expect(paretoView(modelOf(AXES), 0, 1200)?.points).toEqual(whole?.points);
  });

  test('is absent for a run with one objective or with more than two', () => {
    expect(paretoView(modelOf(AXES.slice(0, 1)), 0, null)).toBeNull();
    expect(paretoView(modelOf([]), 0, null)).toBeNull();
    expect(paretoView(modelOf([...AXES, {name: 'memory', direction: 'min'}]), 0, null)).toBeNull();
  });
});

/** What core states for the run: the best on each axis and the input's value there. */
function statedBy(best: Readonly<Record<string, number>>) {
  return new Map(
    Object.entries(best).map(([name, value]) => [
      name,
      {value, input: name === 'p99_latency' ? 20 : 1000, points: [], best: []},
    ]),
  );
}

describe('the performance row of a run with two objectives', () => {
  test('has a tile per objective: the best on its axis against the input, where lower is better too', () => {
    const tiles = heroView({
      ...modelOf(AXES),
      series: statedBy({total_ops_per_sec: 2500, p99_latency: 10}),
    }).headlines;

    expect(tiles.map(t => t.name)).toEqual(['Total ops/sec', 'P99 latency']);
    // the best throughput is 2,500 against 1,000; the best latency is 10 ms against 20 ms
    expect(tiles.map(t => t.ratio)).toEqual(['2.50×', '2.00×']);
    expect(tiles.map(t => t.note)).toEqual(['faster than input', 'faster than input']);
    expect(tiles[0]?.detail).toBe('2,500 ops/s · input 1,000 ops/s');
    expect(tiles[1]?.detail).toBe('10 ms · input 20 ms');
  });

  test('reads each best from core, never from the ideas that passed', () => {
    // Idea 2 passed with 2,500 and idea 4 with 10 ms; core states other bests (a within-noise pass).
    const stated = heroView({
      ...modelOf(AXES),
      series: statedBy({total_ops_per_sec: 1500, p99_latency: 18}),
    }).headlines;
    expect(stated.map(t => t.ratio)).toEqual(['1.50×', '1.11×']);
    const unstated = heroView(modelOf(AXES)).headlines;
    expect(unstated.map(t => t.ratio)).toEqual([null, null]);
  });

  test('keeps one tile for a run with one objective', () => {
    expect(heroView(modelOf(AXES.slice(0, 1))).headlines).toHaveLength(1);
  });
});
