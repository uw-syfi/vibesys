import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace, traceFromSnapshot, traceSpans} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  MAP_FILTERS,
  type MapRow,
  type MapState,
  mapDetail,
  mapForest,
  mapRows,
  type RunView,
  scrubberView,
  scrubPositions,
  selectedSpan,
  type TraceLog,
  traceAt,
  viewAtPosition,
} from './index.js';
import {titleOf} from './span-words.js';

const STATUSES: readonly Span['status'][] = [
  'pending',
  'running',
  'waiting',
  'done',
  'failed',
  'stopped',
];
const ENDINGS: readonly Span['status'][] = ['done', 'failed', 'stopped'];

function stamp(sequence: number): string {
  const seconds = String(sequence % 60).padStart(2, '0');
  const minutes = String(Math.floor(sequence / 60) % 60).padStart(2, '0');
  return `2026-10-15T16:${minutes}:${seconds}+00:00`;
}

/**
 * A run's events as the dynamic search's trace writes them: the root, the input's measurement,
 * planning calls, workstreams (some building on earlier ones), agent turns and gates under them,
 * spans of a kind no client knows (some with attributes that are not JSON), updates that wait,
 * retry and relabel, endings with verdicts, and events that carry no span.
 */
class Journal {
  readonly events: RunEvent[] = [];
  readonly #random: Seeded;
  readonly #spans = new Map<string, Span>();
  readonly #open: string[] = [];
  #sequence = 0;
  #calls = 0;
  #workstreams: string[] = [];

  constructor(random: Seeded) {
    this.#random = random;
    this.#start({id: 'flow:root#1', parent: null, type: 'flow', name: 'dynamic'});
    this.#start({
      id: 'flow:root/input#1',
      parent: 'flow:root#1',
      type: 'flow',
      name: 'measure_the_input',
    });
  }

  run(steps: number): RunEvent[] {
    for (let step = 0; step < steps; step += 1) this.#step();
    return this.events;
  }

  #step(): void {
    const random = this.#random;
    switch (random.int(9)) {
      case 0:
        this.#planningCall();
        break;
      case 1:
        this.#workstream();
        break;
      case 2:
      case 3:
        this.#operation();
        break;
      case 4:
        this.#unknown();
        break;
      case 5:
      case 6:
        this.#update();
        break;
      case 7:
        this.#end();
        break;
      default:
        this.#other();
        break;
    }
  }

  #planningCall(): void {
    this.#calls += 1;
    const id = `flow:root/plan#${this.#calls}`;
    this.#start({
      id,
      parent: 'flow:root#1',
      type: 'flow',
      name: 'plan',
      label: {
        kind: 'planning_call',
        title: `planning call ${this.#calls}`,
        attrs_json: JSON.stringify({call: this.#calls}),
      },
    });
  }

  #workstream(): void {
    const n = this.#workstreams.length + 1;
    const id = `flow:root/ws-${n}#1`;
    const builds =
      this.#workstreams.length > 0 && this.#random.bool()
        ? [this.#random.pick(this.#workstreams)]
        : [];
    this.#start({
      id,
      parent: 'flow:root#1',
      type: 'flow',
      name: 'workstream',
      label: {
        kind: 'workstream',
        title: `Hypothesis ${n}`,
        attrs_json: JSON.stringify({hypothesis_id: `W${n}`, parent_revision: 'r0'}),
        links: builds.map(to => ({rel: 'builds_on', to})),
      },
    });
    this.#workstreams.push(id);
  }

  #operation(): void {
    const parents = [...this.#spans.values()].filter(
      span => span.label?.kind === 'workstream' || span.label?.kind === 'planning_call',
    );
    if (parents.length === 0) {
      this.#other();
      return;
    }
    const parent = this.#random.pick(parents);
    const gate = parent.label?.kind === 'workstream' && this.#random.bool();
    const label = gate
      ? {
          kind: 'gate',
          title: 'correctness + benchmark',
          attrs_json: JSON.stringify({stage: 'correctness,benchmark'}),
        }
      : {
          kind: 'agent_turn',
          title: 'implementer',
          attrs_json: JSON.stringify({role: 'implementer', model: 'claude-sonnet-5'}),
        };
    this.#start({
      id: `op:${this.#sequence + 1}`,
      parent: parent.id,
      type: 'operation',
      name: 'turn',
      label,
    });
  }

  #unknown(): void {
    const parent = this.#random.pick([...this.#spans.keys()]);
    const attrs = this.#random.bool() ? JSON.stringify({depth: 3, note: 'x'}) : 'not json';
    this.#start({
      id: `op:${this.#sequence + 1}`,
      parent,
      type: 'operation',
      name: 'probe',
      label: this.#random.bool() ? {kind: 'probe_kind', title: 'a probe', attrs_json: attrs} : null,
    });
  }

  #update(): void {
    if (this.#open.length === 0) {
      this.#other();
      return;
    }
    const id = this.#random.pick(this.#open);
    const span = this.#spans.get(id) as Span;
    const status = this.#random.pick(STATUSES.filter(each => !ENDINGS.includes(each)));
    const at = this.#sequence + 1;
    const waiting =
      status === 'waiting'
        ? [
            {
              kind: this.#random.pick(['slot', 'operation', 'time', 'stop'] as const),
              ref: this.#random.pick([...this.#spans.keys()]),
              since: at,
            },
          ]
        : [];
    const attempts = this.#random.bool()
      ? [
          {
            n: 1,
            of: 3,
            started_at: at,
            ended_at: at,
            outcome: 'again' as const,
            reason: 'build failed',
          },
          {n: 2, of: 3, started_at: at},
        ]
      : (span.attempts ?? []);
    this.#push('span_updated', {...span, status, waiting_on: waiting, attempts});
  }

  #end(): void {
    if (this.#open.length === 0) {
      this.#other();
      return;
    }
    const id = this.#open.splice(this.#random.int(this.#open.length), 1)[0] as string;
    const span = this.#spans.get(id) as Span;
    const status = this.#random.pick(ENDINGS);
    const verdicts: Readonly<Record<string, readonly string[]>> = {
      workstream: ['accepted', 'rejected'],
      gate: ['passed', 'slower', 'failed_check'],
    };
    const choices = verdicts[span.label?.kind ?? ''];
    const label =
      span.label === undefined || span.label === null || choices === undefined
        ? (span.label ?? null)
        : {...span.label, verdict: this.#random.pick(choices), reason: 'slower_than_input'};
    const failure =
      status === 'failed' ? {kind: 'outcome' as const, message: 'benchmark timed out'} : null;
    this.#push('span_ended', {
      ...span,
      status,
      ended_at: this.#sequence + 1,
      waiting_on: [],
      failure,
      label,
    });
  }

  #other(): void {
    this.#sequence += 1;
    this.events.push({
      protocol_version: 1,
      run_id: 'run-map',
      sequence: this.#sequence,
      timestamp: stamp(this.#sequence),
      type: 'round_finished',
    } as RunEvent);
  }

  #start(span: Omit<Span, 'status' | 'started_at'>): void {
    this.#open.push(span.id);
    this.#push('span_started', {...span, status: 'running', started_at: this.#sequence + 1});
  }

  #push(type: 'span_started' | 'span_updated' | 'span_ended', span: Span): void {
    this.#spans.set(span.id, span);
    this.#sequence += 1 + this.#random.int(2);
    this.events.push({
      protocol_version: 1,
      run_id: 'run-map',
      sequence: this.#sequence,
      timestamp: stamp(this.#sequence),
      type,
      data: {kind: type, span},
    } as RunEvent);
  }
}

function journal(seed: number): RunEvent[] {
  const random = new Seeded(seed);
  return new Journal(random).run(5 + random.int(40));
}

/** The snapshot the server takes at `sequence`: the latest of each span, in start order. */
function snapshotAt(events: readonly RunEvent[], sequence: number): RunSnapshot {
  const spans = new Map<string, Span>();
  for (const event of events) {
    if ((event.sequence ?? 0) > sequence) break;
    const data = event.data;
    if (data && 'span' in data) spans.set(data.span.id, data.span);
  }
  return {
    protocol_version: 1,
    run_id: 'run-map',
    sequence,
    status: 'running',
    spans: [...spans.values()],
  } as RunSnapshot;
}

/** A history that starts from a snapshot somewhere in the run (or from its start). */
function history(seed: number): TraceLog {
  const events = journal(seed);
  const random = new Seeded(seed * 31);
  const cut = random.int(3) === 0 ? random.int(events.length) : 0;
  const at = cut === 0 ? 0 : (events[cut - 1]?.sequence ?? 0);
  const base = at === 0 ? EMPTY_TRACE : traceFromSnapshot(snapshotAt(events, at));
  return {base, baseOverview: null, events: events.filter(event => (event.sequence ?? 0) > at)};
}

function mapState(seed: number, trace: ReturnType<typeof traceAt>): MapState {
  const random = new Seeded(seed * 17);
  const ids = [...trace.spans.keys()];
  return {
    filter: random.pick(MAP_FILTERS),
    open: new Set(ids.filter(() => random.bool())),
    selected: ids.length > 0 && random.bool() ? random.pick(ids) : random.bool() ? 'op:gone' : null,
  };
}

/** Rows are a well-formed tree: each row one level at most below the row before it. */
function wellFormed(rows: readonly MapRow[]): void {
  let previous = -1;
  for (const row of rows) {
    expect(row.depth).toBeLessThanOrEqual(previous + 1);
    previous = row.depth;
  }
  const ids = rows.map(row => row.id);
  expect(new Set(ids).size).toBe(ids.length);
}

describe('the Map and the scrubber', () => {
  test('the selectors at the last sequence equal the live view', () => {
    for (const seed of SEEDS) {
      const run = history(seed);
      const live = traceAt(run, null);
      const lastAt = scrubPositions(run).at(-1) ?? 0;
      const last = traceAt(run, lastAt);
      const map = mapState(seed, live);
      expect(mapRows(last, map)).toEqual(mapRows(live, map));
      expect(mapDetail(last, map)).toEqual(mapDetail(live, map));
    }
  });

  test('folding a prefix gives the trace at that sequence', () => {
    for (const seed of SEEDS) {
      const run = history(seed);
      const all = [...run.events];
      for (const at of scrubPositions(run)) {
        const expected = snapshotAt(
          // The whole run, so a snapshot at `at` is the server's at that sequence.
          journal(seed),
          at,
        );
        const trace = traceAt(run, at);
        expect(traceSpans(trace)).toEqual(expected.spans ?? []);
        expect(trace).toEqual(
          foldTrace(
            run.base,
            all.filter(event => (event.sequence ?? 0) <= at),
          ),
        );
      }
    }
  });

  test('the tree is a forest with every span once, under an existing parent', () => {
    for (const seed of SEEDS) {
      const run = history(seed);
      for (const at of scrubPositions(run)) {
        const trace = traceAt(run, at);
        const seen: string[] = [];
        const walk = (
          node: ReturnType<typeof mapForest>[number],
          ancestors: readonly string[],
        ): void => {
          seen.push(node.span.id);
          const parent = node.span.parent;
          if (parent !== null) expect(ancestors).toContain(parent);
          for (const child of node.children) walk(child, [...ancestors, node.span.id]);
        };
        for (const root of mapForest(trace)) {
          expect(root.span.parent === null || !trace.spans.has(root.span.parent)).toBe(true);
          walk(root, []);
        }
        expect([...seen].sort()).toEqual([...trace.spans.keys()].sort());
      }
    }
  });

  test('every filter shows a well-formed tree of the spans in its state and their context', () => {
    for (const seed of SEEDS) {
      const run = history(seed);
      const trace = traceAt(run, null);
      for (const filter of MAP_FILTERS) {
        const map: MapState = {filter, open: new Set(trace.spans.keys()), selected: null};
        const rows = mapRows(trace, map);
        wellFormed(rows);
        const shown = new Set(rows.map(row => row.id));
        for (const span of trace.spans.values()) {
          const inState = filter === 'all' || bucketOf(span.status) === filter;
          // Every span in the filter's state is shown (with every group open).
          if (inState) expect(shown.has(span.id)).toBe(true);
        }
      }
    }
  });

  test('the selection falls back to the root when the span is not in the trace at the view; facts are distinct', () => {
    for (const seed of SEEDS.slice(0, 60)) {
      const run = history(seed);
      const live = traceAt(run, null);
      const map = mapState(seed, live);
      for (const at of scrubPositions(run)) {
        const trace = traceAt(run, at);
        const chosen = selectedSpan(trace, map);
        const expected =
          map.selected !== null && trace.spans.has(map.selected)
            ? map.selected
            : (mapForest(trace)[0]?.span.id ?? null);
        expect(chosen?.id ?? null).toBe(expected);
        expect(mapDetail(trace, map)?.id ?? null).toBe(expected);
        const keys = (mapDetail(trace, map)?.facts ?? []).map(fact => fact.key);
        expect(new Set(keys).size).toBe(keys.length);
      }
    }
  });

  test('the scrubber moves between its positions and back to live', () => {
    for (const seed of SEEDS) {
      const run = history(seed);
      const positions = scrubPositions(run);
      for (const ended of [false, true]) {
        const live = scrubberView(run, {at: 'live'}, ended);
        if (positions.length === 1) {
          expect(live).toBeNull();
          continue;
        }
        expect(live).toMatchObject({
          value: positions.length - 1,
          last: positions.length - 1,
          latest: true,
        });
        for (let index = 0; index < positions.length; index += 1) {
          const view: RunView = viewAtPosition(run, index);
          const shown = scrubberView(run, view, ended);
          expect(shown?.value).toBe(index);
          expect(shown?.latest).toBe(index === positions.length - 1);
        }
      }
    }
  });
});

function bucketOf(status: Span['status']): string {
  if (status === 'pending' || status === 'waiting') return 'waiting';
  if (status === 'done' || status === 'stopped') return 'finished';
  return status;
}

const ROOT = 'flow:root#1';

function event(sequence: number, type: string, span: Span): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run-map',
    sequence,
    timestamp: stamp(sequence),
    type,
    data: {kind: type, span},
  } as RunEvent;
}

function span(id: string, parent: string | null, extra: Partial<Span> = {}): Span {
  return {id, parent, type: 'flow', name: id, status: 'running', started_at: 100, ...extra};
}

const workstream = (n: number, extra: Partial<Span> = {}, links: readonly string[] = []): Span =>
  span(`flow:root/ws-${n}#1`, ROOT, {
    label: {
      kind: 'workstream',
      title: `Hypothesis ${n}`,
      attrs_json: JSON.stringify({hypothesis_id: `W${n}`, parent_revision: 'r0'}),
      links: links.map(to => ({rel: 'builds_on', to})),
    },
    ...extra,
  });

const plan = (n: number): Span =>
  span(`flow:root/plan#${n}`, ROOT, {
    status: 'done',
    label: {
      kind: 'planning_call',
      title: `planning call ${n}`,
      attrs_json: JSON.stringify({call: n}),
    },
  });

describe('a dynamic run on the Map', () => {
  const events = [
    event(1, 'span_started', span(ROOT, null, {name: 'dynamic'})),
    event(
      2,
      'span_started',
      span('flow:root/input#1', ROOT, {name: 'measure_the_input', status: 'done'}),
    ),
    event(3, 'span_started', plan(1)),
    event(
      4,
      'span_started',
      workstream(1, {
        status: 'done',
        label: {
          kind: 'workstream',
          title: 'Lock-free ring',
          verdict: 'accepted',
          attrs_json: JSON.stringify({hypothesis_id: 'W1', parent_revision: 'r0'}),
        },
      }),
    ),
    event(
      5,
      'span_started',
      workstream(2, {
        status: 'done',
        label: {
          kind: 'workstream',
          title: 'Padding',
          verdict: 'rejected',
          reason: 'slower_than_input',
        },
      }),
    ),
    event(6, 'span_started', plan(2)),
    event(7, 'span_started', workstream(5, {}, ['flow:root/ws-1#1'])),
    event(
      8,
      'span_started',
      span('op:turn', 'flow:root/ws-5#1', {
        type: 'operation',
        status: 'done',
        label: {
          kind: 'agent_turn',
          title: 'implementer',
          attrs_json: JSON.stringify({role: 'implementer', model: 'claude-sonnet-5'}),
        },
      }),
    ),
    event(
      9,
      'span_started',
      span('op:gate', 'flow:root/ws-5#1', {
        type: 'operation',
        waiting_on: [{kind: 'slot', ref: 'group', since: 160}],
        status: 'waiting',
        label: {
          kind: 'gate',
          title: 'benchmark',
          attrs_json: JSON.stringify({stage: 'benchmark', metric_value: 41.2, vs_baseline: 1.18}),
        },
      }),
    ),
    event(
      10,
      'span_started',
      span('op:odd', ROOT, {
        type: 'operation',
        label: {kind: 'cache_warmup', title: 'warm the cache', attrs_json: '{"entries": 12}'},
      }),
    ),
  ];
  const run: TraceLog = {base: EMPTY_TRACE, baseOverview: null, events};
  const all: MapState = {filter: 'all', open: new Set(), selected: null};

  test('orders run, baseline, planning calls, workstreams and their steps; finished work folds', () => {
    const rows = mapRows(traceAt(run, null), all).map(
      row => `${'  '.repeat(row.depth)}${row.title}`,
    );
    expect(rows).toEqual([
      'Run · dynamic',
      '  measure_the_input',
      '  Planning call 1',
      '    2 finished workstreams',
      '  Planning call 2',
      '    W5 · Hypothesis 5',
      '      implementer · claude-sonnet-5',
      '      Gate · benchmark',
      '  warm the cache',
    ]);
    const opened = mapRows(traceAt(run, null), {
      ...all,
      open: new Set(['flow:root/plan#1']),
    });
    expect(opened.map(row => row.title)).toContain('W1 · Lock-free ring');
    const ws5 = opened.find(row => row.id === 'flow:root/ws-5#1');
    expect(ws5?.kind === 'span' ? ws5.chips.map(chip => chip.label) : []).toEqual([
      'builds on W1',
      'Running',
    ]);
  });

  test('the filter keeps the context of what it shows', () => {
    const waiting = mapRows(traceAt(run, null), {...all, filter: 'waiting'});
    expect(waiting.map(row => row.title)).toEqual([
      'Run · dynamic',
      'Planning call 2',
      'W5 · Hypothesis 5',
      'Gate · benchmark',
    ]);
  });

  test('details read the known kinds and an unknown kind plainly', () => {
    const live = traceAt(run, null);
    const gate = mapDetail(live, {...all, selected: 'op:gate'});
    expect(gate?.kind).toBe('Gate');
    expect(gate?.facts).toContainEqual({
      key: 'Waiting on',
      value: 'Its steps to finish, since 1m into the run',
    });
    expect(gate?.facts).toContainEqual({key: 'Metric value', value: '41.2', mono: true});
    const odd = mapDetail(live, {...all, selected: 'op:odd'});
    expect(odd).toMatchObject({kind: 'Cache warmup', title: 'warm the cache'});
    expect(odd?.facts).toContainEqual({key: 'Entries', value: '12', mono: true});
    const rejected = mapDetail(live, {...all, selected: 'flow:root/ws-2#1'});
    expect(rejected?.facts).toContainEqual({key: 'Verdict', value: 'Rejected · Slower than input'});
    const ws5 = mapDetail(live, {...all, selected: 'flow:root/ws-5#1'});
    expect(ws5?.links).toEqual([{label: 'Builds on W1', to: 'flow:root/ws-1#1'}]);
  });

  test('the scrubber ticks at planning calls and reads the time and call at its playhead', () => {
    const view = scrubberView(run, {at: 4}, false);
    expect(view).toMatchObject({
      value: 4,
      last: 10,
      latest: false,
      latestLabel: 'Live',
      backLabel: 'Back to live',
    });
    expect(view?.position).toBe('16:00:04 · planning call 1');
    expect(view?.ticks).toEqual([
      {at: 3, label: 'Call 1'},
      {at: 6, label: 'Call 2'},
    ]);
    expect(scrubberView(run, {at: 'live'}, true)).toMatchObject({
      latestLabel: 'End',
      backLabel: 'Jump to end',
    });
    expect(mapRows(traceAt(run, 2), all).map(row => row.title)).toEqual([
      'Run · dynamic',
      'measure_the_input',
    ]);
  });
});

describe('a gate that builds the candidate', () => {
  test('reads its build stage as the candidate build, its other stages as they are', () => {
    const gate = (title: string) =>
      span('op:gate', ROOT, {type: 'operation', label: {kind: 'gate', title}});

    expect(titleOf(gate('build + accuracy + benchmark'))).toBe(
      'Gate · candidate build + accuracy + benchmark',
    );
    expect(titleOf(gate('accuracy + benchmark'))).toBe('Gate · accuracy + benchmark');
  });
});
