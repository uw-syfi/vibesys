import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {bestPathView, IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {EMPTY_TRACE_LOG, type TraceLog, traceAt, traceLog, traceStep} from './live.js';
import {
  clockCarried,
  heroView,
  logStreamToFollow,
  nowView,
  resolveZoom,
  shownLogScope,
} from './run-level.js';
import {createRunReader} from './run-model.js';
import {type RunPageState, runPageInitial, runPageStep} from './run-page.js';
import {scopedLogs} from './scoped-logs.js';
import {scrubberView} from './scrub.js';
import {type Route, RUN_MODES, RUN_ROUTE} from './zoom.js';

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
  readonly verdict?: 'passed' | 'failed';
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
      attrs_json: JSON.stringify({
        hypothesis_id: `h${of.n}`,
        idea: `try ${of.title}`,
        round: of.n,
        base_round: of.on ?? null,
        ...(status === 'done' && of.verdict === 'passed' ? {revision: `c${of.n}`} : {}),
      }),
    },
  } as Span;
}

/** The adoption span: the search's current best (a revision), adopted once it has a verdict. */
function adopting(candidate: string | null, verdict: 'adopted' | null): Span {
  return {
    id: 'op:adopt',
    parent: ROOT.id,
    type: 'operation',
    name: 'adopt',
    status: verdict === null ? 'running' : 'done',
    started_at: 500,
    ended_at: verdict === null ? null : 500,
    label: {
      kind: 'adoption',
      ...(verdict === null ? {} : {verdict}),
      attrs_json: JSON.stringify({candidate}),
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
      verdict: of.verdict === 'passed' ? 'passed' : 'slower',
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

const A: IdeaOf = {n: 1, title: 'ring', verdict: 'passed', value: 150, vs: 1.5};
const B: IdeaOf = {n: 2, title: 'ring-pad', on: 1, verdict: 'passed', value: 200, vs: 2};
const C: IdeaOf = {n: 3, title: 'spin', on: 2, verdict: 'failed', value: 120, vs: 1.2};
const D: IdeaOf = {n: 4, title: 'arena', on: 2};

function run(adoption: Span = adopting('c2', null)): RunEvent[] {
  return events([
    ROOT,
    ...finished(A),
    ...finished(B),
    ...finished(C),
    workstream(D, 'running'),
    turn(D, 'running'),
    adoption,
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

describe('the run clock between events', () => {
  test('carries the live position past the last event on the window clock, not a past one', () => {
    const log = logOf(run());
    const model = createRunReader().read(log, null);
    const latest = model.latest ?? 0;
    expect(clockCarried(model, null)).toBe(0);
    expect(clockCarried(model, latest - 5)).toBe(0);
    // Two minutes and five seconds after the run's last word, the live position says so.
    const carried = clockCarried(model, latest + 125);
    expect(carried).toBe(125);
    expect(scrubberView(log, {at: 'live'}, false, carried)?.position).toStartWith('00:02:05');
    expect(scrubberView(log, {at: 'live'}, false, 0)?.position).toStartWith('00:00:00');
    // An ended run's end and a past position read their event's own time.
    expect(scrubberView(log, {at: 'live'}, true, carried)?.position).toStartWith('00:00:00');
    expect(scrubberView(log, {at: 3}, false, carried)?.position).toStartWith('00:00:00');
  });
});

describe('fresh ideas', () => {
  test('are the ideas the events just read changed or brought, never the history', () => {
    const all = run();
    const reader = createRunReader();
    // The page's first read is its history: nothing flashes.
    const first = logOf(all.slice(0, 7));
    expect(reader.read(first, null).changed.size).toBe(0);
    // ring-pad finishes and spin starts: both flash, ring does not.
    const grown = traceStep(first, {kind: 'events', events: all.slice(7, 12)});
    const model = reader.read(grown, null);
    expect([...model.changed].sort()).toEqual(['flow:w2/#1', 'flow:w3/#1']);
    expect(
      ideasView(model, IDEAS_FILTER_DEFAULT).rows.find(row => row.title === 'ring')?.fresh,
    ).toBe(false);
    // A past sequence shows no news.
    expect(reader.read(grown, 3).changed.size).toBe(0);
  });
});

describe('the run level', () => {
  const model = createRunReader().read(logOf(run()), null);

  test('ideas show running first, then newest first, the failed folded away', () => {
    const view = ideasView(model, IDEAS_FILTER_DEFAULT);
    expect(view.rows.map(row => row.title)).toEqual(['arena', 'ring-pad', 'ring']);
    expect(view.hiddenFailed).toBe(1);
    expect(view.counts).toEqual({running: 1, passed: 2, failed: 1});
    const ringPad = view.rows[1];
    expect(ringPad?.badges).toEqual([{label: 'best', tone: 'ok'}]);
    expect(ringPad?.trail).toEqual({label: 'ring', parent: 'flow:w1/#1'});
    expect(view.rows[2]?.trail).toEqual({label: 'input', parent: null});
    expect(view.rows.map(row => row.stripe)).toEqual([false, true, true]);
    expect(ideasView(model, IDEAS_FILTER_DEFAULT)).toBe(view);
  });

  test('the filter shows the failed and searches titles and ideas', () => {
    const all = ideasView(model, {...IDEAS_FILTER_DEFAULT, failed: true});
    expect(all.rows.map(row => row.title)).toEqual(['arena', 'spin', 'ring-pad', 'ring']);
    expect(all.rows[1]?.figure).toBe('1.20×');
    const found = ideasView(model, {...IDEAS_FILTER_DEFAULT, failed: true, query: 'try spin'});
    expect(found.rows.map(row => row.title)).toEqual(['spin']);
  });

  test('the best path names the winning chain, and the hero its ratio', () => {
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
        doing: 'implementer · implementer',
        context: 'arena',
        since: 241,
        open: {kind: 'step', idea: 'flow:w4/#1', step: 'flow:w4/turn/#1'},
      },
    ]);
  });

  test('Now says a running idea waits between steps, and when its running attempt started', () => {
    // ring tries again (the same hypothesis): its implementer runs, finishes and yields to an
    // evaluation, as the chelan4 run's round 4 did.
    const again = {...workstream(A, 'running'), id: 'flow:w1/#2', started_at: 600};
    const implement = {
      ...turn(A, 'running'),
      id: 'flow:w1/#2/turn',
      parent: again.id,
      started_at: 601,
    };
    const yielded: Span = {
      ...again,
      waiting_on: [
        {kind: 'operation', ref: 'resume of inv:implementer/evaluation', since: 640},
        {kind: 'time', ref: 'timer', since: 600},
      ],
    };
    const rows = (versions: readonly Span[]) =>
      nowView(
        createRunReader().read(logOf(events([ROOT, ...finished(A), ...versions])), null),
      ).find(row => row.context === 'ring');
    expect(rows([again])).toMatchObject({doing: 'starting', since: 600});
    expect(rows([again, implement])).toMatchObject({
      doing: 'implementer · implementer',
      since: 601,
    });
    const done = {...implement, status: 'done' as const, ended_at: 639};
    expect(rows([again, implement, done, yielded])).toMatchObject({
      doing: 'waiting after implementer',
      since: 640,
      open: {kind: 'step', idea: 'flow:w1/#1', step: 'flow:w1/#2/turn'},
    });
    // Without a wait it has said, it waits since its last step ended.
    expect(rows([again, implement, done])).toMatchObject({
      doing: 'waiting after implementer',
      since: 639,
    });
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

describe('following the Logs face', () => {
  const model = createRunReader().read(logOf(run()), null);

  function stream(id: string, span: string | null, live: boolean) {
    return {
      id,
      owner: {kind: 'gate_stage', gate: 'build', execution_id: id, attempt: 0, span_id: span},
      command: 'cmd',
      retained_start: 0,
      size: 0,
      outcome: live ? null : 'ended',
    };
  }

  function page(): RunPageState {
    const initial = runPageInitial({
      identity: {host: 'ssh:chelan4', runId: 'run'},
      title: {project: 'queue-rs', task: 'mpmc'},
      hostLabel: 'chelan4',
      unavailable: {},
    });
    const streams = [
      stream('s-run', null, false),
      stream('s-ring-pad', 'flow:w2/turn/#1', false),
      stream('s-arena', 'flow:w4/turn/#1', true),
    ];
    return {...initial, logs: {kind: 'listed', instanceId: 'i', streams}} as RunPageState;
  }

  const ROUTES: readonly Route[] = RUN_MODES.flatMap(mode => [
    {...RUN_ROUTE, mode},
    {...RUN_ROUTE, mode, idea: 'flow:w2/#1'},
    {...RUN_ROUTE, mode, idea: 'flow:w2/#1', step: 'flow:w2/turn/#1'},
    {...RUN_ROUTE, mode, idea: 'flow:w2/#1', step: 'flow:w2/turn/#1', item: 'tool-1'},
    {...RUN_ROUTE, mode, idea: 'flow:w4/#1'},
    {...RUN_ROUTE, mode, idea: 'gone'},
  ]);

  test('selects the stream the face shows, once, and nothing when no Logs face is on screen', () => {
    for (const route of ROUTES) {
      let state = page();
      let dispatched = 0;
      for (
        let follow = logStreamToFollow(state, model, route);
        follow !== null && dispatched < 3;
      ) {
        state = runPageStep(state, {type: 'select-stream', streamId: follow}).state;
        dispatched += 1;
        follow = logStreamToFollow(state, model, route);
      }
      const scope = shownLogScope(route, resolveZoom(model, route));
      if (route.mode !== 'logs' || scope === null) {
        expect(dispatched).toBe(0);
        continue;
      }
      expect(dispatched).toBeLessThanOrEqual(1);
      expect(state.selected).toBe(scopedLogs(state, scope).shown);
    }
  });

  test('an idea follows its own live stream; a step its own; an item shows no Logs face', () => {
    const at = (route: Route) => logStreamToFollow(page(), model, {...route, mode: 'logs'});
    expect(at({...RUN_ROUTE, idea: 'flow:w4/#1'})).toBe('s-arena');
    expect(at({...RUN_ROUTE, idea: 'flow:w2/#1', step: 'flow:w2/turn/#1'})).toBe('s-ring-pad');
    expect(at({...RUN_ROUTE, idea: 'flow:w2/#1', step: 'flow:w2/turn/#1', item: 't'})).toBeNull();
    expect(at({...RUN_ROUTE, idea: 'flow:w1/#1'})).toBeNull();
  });
});
