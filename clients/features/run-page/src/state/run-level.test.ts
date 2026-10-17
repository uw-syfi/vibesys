import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {bestPathView, IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {EMPTY_TRACE_LOG, type TraceLog, traceAt, traceLog, traceStep} from './live.js';
import {heroView, nowView, resolveZoom} from './run-level.js';
import {createRunReader} from './run-model.js';
import {RUN_ROUTE} from './zoom.js';

const ROOT: Span = {
  id: 'flow:search/#1',
  parent: null,
  type: 'flow',
  name: 'search',
  status: 'running',
  started_at: 0,
};

const ENDED = new Set(['done', 'failed', 'stopped']);

function events(versions: readonly Span[], from = 1): RunEvent[] {
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
      run_id: 'run',
      sequence: from + index,
      timestamp: '2026-01-01T00:00:00Z',
      type,
      data: {kind: type, span},
    } as RunEvent;
  });
}

interface IdeaOf {
  readonly n: number;
  readonly title: string;
  readonly on?: number;
  readonly verdict?: 'accepted' | 'rejected';
  readonly value?: number;
  readonly vs?: number;
}

function workstream(of: IdeaOf, status: Span['status']): Span {
  const links = of.on === undefined ? [] : [{rel: 'builds_on', to: `flow:w${of.on}/#1`}];
  return {
    id: `flow:w${of.n}/#1`,
    parent: ROOT.id,
    type: 'flow',
    name: 'workstream',
    status,
    started_at: of.n * 60,
    ended_at: ENDED.has(status) ? of.n * 60 + 50 : null,
    label: {
      kind: 'workstream',
      title: of.title,
      ...(status === 'done' && of.verdict !== undefined ? {verdict: of.verdict} : {}),
      links,
      attrs_json: JSON.stringify({hypothesis_id: `h${of.n}`, idea: `try ${of.title}`}),
    },
  } as Span;
}

function turn(of: IdeaOf, status: Span['status']): Span {
  return {
    id: `flow:w${of.n}/turn/#1`,
    parent: `flow:w${of.n}/#1`,
    type: 'operation',
    name: 'turn',
    status,
    started_at: of.n * 60 + 1,
    ended_at: ENDED.has(status) ? of.n * 60 + 30 : null,
    label: {
      kind: 'agent_turn',
      title: 'implementer',
      attrs_json: JSON.stringify({
        agent: `impl-${of.n}`,
        role: 'implementer',
        started_by: ['orch'],
      }),
    },
  };
}

function gate(of: IdeaOf): Span {
  return {
    id: `flow:w${of.n}/gate/#1`,
    parent: `flow:w${of.n}/#1`,
    type: 'operation',
    name: 'measure',
    status: 'done',
    started_at: of.n * 60 + 31,
    ended_at: of.n * 60 + 40,
    label: {
      kind: 'gate',
      title: 'benchmark',
      verdict: of.verdict === 'accepted' ? 'passed' : 'slower',
      attrs_json: JSON.stringify({stage: 'benchmark', metric_value: of.value, vs_baseline: of.vs}),
    },
  };
}

/** A finished idea's versions: started, its turn and gate, then decided. */
function finished(of: IdeaOf): Span[] {
  return [
    workstream(of, 'running'),
    turn(of, 'running'),
    turn(of, 'done'),
    gate(of),
    workstream(of, 'done'),
  ];
}

const A: IdeaOf = {n: 1, title: 'ring', verdict: 'accepted', value: 150, vs: 1.5};
const B: IdeaOf = {n: 2, title: 'ring-pad', on: 1, verdict: 'accepted', value: 200, vs: 2};
const C: IdeaOf = {n: 3, title: 'spin', on: 2, verdict: 'rejected', value: 120, vs: 1.2};
const D: IdeaOf = {n: 4, title: 'arena', on: 2};

function run(): RunEvent[] {
  return events([
    ROOT,
    ...finished(A),
    ...finished(B),
    ...finished(C),
    workstream(D, 'running'),
    turn(D, 'running'),
  ]);
}

function logOf(list: readonly RunEvent[]): TraceLog {
  return traceStep(EMPTY_TRACE_LOG, {kind: 'events', events: list});
}

describe('the trace at the live end', () => {
  test('is the log’s own fold, not a refold per read', () => {
    const log = logOf(run());
    expect(traceAt(log, null)).toBe(traceAt(log, null));
    expect(traceAt(log, null)).toBe(log.head);
    // a past sequence still folds from the base
    expect(traceAt(log, 1).spans.size).toBe(1);
  });

  test('folds each new event once, however long the run', () => {
    for (const size of [1, 4]) {
      let units = 0;
      const many = events(Array.from({length: size * 50}, (_, n) => ({...ROOT, started_at: n})));
      const log = traceStep(traceLog(EMPTY_TRACE, null, []), {kind: 'events', events: many});
      const next = events([{...ROOT, status: 'done', ended_at: 1}], many.length + 1);
      traceStep(log, {kind: 'events', events: next}, count => (units += count));
      expect(units).toBe(1);
    }
  });
});

describe('the run reader', () => {
  test('reads the same model while the log is unchanged, and folds only new events', () => {
    let units = 0;
    const reader = createRunReader({count: n => (units += n)});
    const all = run();
    const first = logOf(all.slice(0, 10));
    const model = reader.read(first, null);
    expect(reader.read(first, null)).toBe(model);
    const before = units;
    reader.read(first, null);
    expect(units).toBe(before);
    const grown = traceStep(first, {kind: 'events', events: all.slice(10)});
    const later = reader.read(grown, null);
    expect(later.candidates.candidates.map(item => item.title)).toEqual([
      'ring',
      'ring-pad',
      'spin',
      'arena',
    ]);
    // a reader started on the whole log reads the same run
    expect(createRunReader().read(grown, null).candidates.candidates).toEqual(
      later.candidates.candidates,
    );
  });
});

describe('the run level', () => {
  const model = createRunReader().read(logOf(run()), null);

  test('ideas show running first, then newest first, the rejected folded away', () => {
    const view = ideasView(model, IDEAS_FILTER_DEFAULT);
    expect(view.rows.map(row => row.title)).toEqual(['arena', 'ring-pad', 'ring']);
    expect(view.hiddenRejected).toBe(1);
    expect(view.counts).toEqual({running: 1, kept: 2, rejected: 1});
    const ringPad = view.rows[1];
    expect(ringPad?.badges).toEqual([{label: 'best', tone: 'ok'}]);
    expect(ringPad?.trail).toEqual({label: 'from ring', parent: 'flow:w1/#1'});
    expect(view.rows.map(row => row.stripe)).toEqual([false, true, true]);
    expect(ideasView(model, IDEAS_FILTER_DEFAULT)).toBe(view);
  });

  test('the filter shows the rejected and searches titles and ideas', () => {
    const all = ideasView(model, {...IDEAS_FILTER_DEFAULT, rejected: true});
    expect(all.rows.map(row => row.title)).toEqual(['arena', 'spin', 'ring-pad', 'ring']);
    expect(all.rows[1]?.figure).toBe('1.20×');
    const found = ideasView(model, {...IDEAS_FILTER_DEFAULT, rejected: true, query: 'try spin'});
    expect(found.rows.map(row => row.title)).toEqual(['spin']);
  });

  test('the best path names the kept chain, and the hero its ratio', () => {
    expect(
      bestPathView(model, false)?.crumbs.map(crumb => (crumb.kind === 'idea' ? crumb.label : '…')),
    ).toEqual(['ring', 'ring-pad']);
    const hero = heroView(model);
    expect(hero.ratio).toBe('2.00×');
    expect(hero.input).toBe('input 100');
  });

  test('Now jumps to the running idea’s live step', () => {
    expect(nowView(model)).toEqual([
      {
        id: 'flow:w4/turn/#1',
        title: 'arena',
        summary: 'implementer · implementer',
        since: 241,
        open: {kind: 'step', idea: 'flow:w4/#1', step: 'flow:w4/turn/#1'},
      },
    ]);
  });

  test('a route resolves what the run holds and falls back a level otherwise', () => {
    const route = {...RUN_ROUTE, idea: 'flow:w2/#1', step: 'flow:w2/turn/#1', item: 'tool-1'};
    const zoomed = resolveZoom(model, route);
    expect(zoomed.idea?.title).toBe('ring-pad');
    expect(zoomed.step?.span).toBe('flow:w2/turn/#1');
    expect(zoomed.item).toBe('tool-1');
    const stale = resolveZoom(model, {...route, step: 'gone'});
    expect([stale.idea?.title, stale.step, stale.item]).toEqual(['ring-pad', null, null]);
    expect(resolveZoom(model, {...route, idea: 'gone'}).idea).toBeNull();
  });
});
