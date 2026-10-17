/**
 * A run shaped like the prototype's, for the fake run server: the MPMC queue run's overview and its
 * trace, two planning calls, workstreams W1 to W7 (some building on others), their agent turns and
 * their gates with measured values against the baseline. It is what the session receives: a stream
 * batch that starts each span, and the snapshot that carries the same spans and the overview.
 */
import type {FakeServerSeed} from './fake-server.js';

interface FakeSpan {
  readonly id: string;
  readonly parent: string | null;
  readonly type: 'flow' | 'operation';
  readonly name: string;
  readonly status: 'running' | 'waiting' | 'done' | 'failed';
  readonly started_at: number;
  readonly ended_at?: number;
  readonly label?: {
    readonly kind: string;
    readonly title: string;
    readonly attrs_json: string;
    readonly verdict?: string;
    readonly links?: readonly {readonly rel: string; readonly to: string}[];
  };
}

const OVERVIEW = {
  objective: {
    text: 'Maximize trusted end-to-end throughput (`total_ops_per_sec`) of the MPMC queue. Every candidate must pass the 100-trial correctness check.',
    metric: 'total_ops_per_sec',
    unit: 'ops/s',
    direction: 'max',
    correctness_check: ['cargo', 'test', '--release', 'mpmc_trials'],
  },
};

function label(kind: string, title: string, attrs: object, extra: object = {}) {
  return {kind, title, attrs_json: JSON.stringify(attrs), ...extra};
}

interface Workstream {
  readonly n: number;
  readonly title: string;
  readonly call: string;
  readonly start: number;
  readonly buildsOn: readonly number[];
  readonly verdict: 'passed' | 'failed' | null;
  /** The gate's value and ratio to the baseline, or null while it has not measured. */
  readonly gate: {value: number; ratio: number; verdict: string} | null;
}

const WORKSTREAMS: readonly Workstream[] = [
  {
    n: 1,
    title: 'Lock-free ring with per-slot sequence numbers',
    call: 'p1',
    start: 420,
    buildsOn: [],
    verdict: 'passed',
    gate: {value: 1.9e7, ratio: 1.9, verdict: 'passed'},
  },
  {
    n: 2,
    title: 'Cache-line padding and batched enqueue',
    call: 'p1',
    start: 420,
    buildsOn: [],
    verdict: 'passed',
    gate: {value: 1.55e7, ratio: 1.55, verdict: 'passed'},
  },
  {
    n: 3,
    title: 'Spin then park idle consumers',
    call: 'p1',
    start: 900,
    buildsOn: [],
    verdict: 'failed',
    gate: {value: 0.85e7, ratio: 0.85, verdict: 'slower'},
  },
  {
    n: 4,
    title: 'Larger batch size',
    call: 'p1',
    start: 960,
    buildsOn: [],
    verdict: 'failed',
    gate: {value: 0, ratio: 0, verdict: 'failed_check'},
  },
  {
    n: 5,
    title: 'Combine W1 ring with W2 padding',
    call: 'p2',
    start: 1260,
    buildsOn: [1, 2],
    verdict: null,
    gate: {value: 2.4e7, ratio: 2.4, verdict: 'passed'},
  },
  {
    n: 6,
    title: 'Wait-free consumer path',
    call: 'p2',
    start: 1260,
    buildsOn: [1],
    verdict: null,
    gate: null,
  },
  {
    n: 7,
    title: 'Per-core queues with stealing',
    call: 'p2',
    start: 1500,
    buildsOn: [5, 1],
    verdict: null,
    gate: null,
  },
];

function workstreamSpans(w: Workstream): FakeSpan[] {
  const id = `w${w.n}`;
  const ended = w.verdict === null ? {} : {ended_at: w.start + 600};
  const workstream: FakeSpan = {
    id,
    parent: w.call,
    type: 'flow',
    name: 'workstream',
    status: w.verdict === null ? (w.n === 7 ? 'waiting' : 'running') : 'done',
    started_at: w.start,
    ...ended,
    label: label(
      'workstream',
      w.title,
      {
        hypothesis_id: `W${w.n}`,
        parent_revision: 'r0',
        base_round: null,
        ...(w.verdict === 'passed' ? {revision: `r${w.n}`} : {}),
      },
      {
        ...(w.verdict === null ? {} : {verdict: w.verdict}),
        ...(w.verdict === 'failed' ? {reason: 'turned_down'} : {}),
        links: w.buildsOn.map(other => ({rel: 'builds_on', to: `w${other}`})),
      },
    ),
  };
  if (w.n === 7) return [workstream];
  const turn: FakeSpan = {
    id: `${id}-impl`,
    parent: id,
    type: 'operation',
    name: 'agent turn',
    status: w.n === 6 ? 'running' : 'done',
    started_at: w.start + 1,
    ...(w.n === 6 ? {} : {ended_at: w.start + 380}),
    label: label('agent_turn', 'Implementer', {role: 'implementer', model: 'claude-sonnet-5'}),
  };
  if (w.gate === null) return [workstream, turn];
  const gate: FakeSpan = {
    id: `${id}-gate`,
    parent: id,
    type: 'flow',
    name: 'gate',
    status: w.verdict === null ? 'running' : 'done',
    started_at: w.start + 390,
    ...(w.verdict === null ? {} : {ended_at: w.start + 590}),
    label: label(
      'gate',
      'Correctness, then benchmark',
      {stage: 'benchmark', metric_value: w.gate.value, vs_baseline: w.gate.ratio},
      {verdict: w.gate.verdict},
    ),
  };
  return [workstream, turn, gate];
}

function spans(): FakeSpan[] {
  const call = (id: string, n: number, start: number): FakeSpan => ({
    id,
    parent: 'run',
    type: 'operation',
    name: 'planning call',
    status: 'done',
    started_at: start,
    ended_at: start + 40,
    label: label('planning_call', `Planning call ${n}`, {call: n}),
  });
  return [
    {id: 'run', parent: null, type: 'flow', name: 'dynamic', status: 'running', started_at: 0},
    {
      id: 'base',
      parent: 'run',
      type: 'flow',
      name: 'gate',
      status: 'done',
      started_at: 330,
      ended_at: 380,
      label: label(
        'gate',
        'Baseline',
        {stage: 'benchmark', metric_value: 1e7, vs_baseline: 1},
        {verdict: 'passed'},
      ),
    },
    call('p1', 1, 385),
    call('p2', 2, 1220),
    {
      id: 'adopt',
      parent: 'run',
      type: 'operation',
      name: 'adopt',
      status: 'running',
      started_at: 1100,
      label: label('adoption', 'r1', {candidate: 'r1', metric_value: 1.9e7, vs_baseline: 1.9}),
    },
    ...WORKSTREAMS.flatMap(workstreamSpans),
  ];
}

/** `spans` with the wire's ids: `flow:` or `op:` by the span's type, in ids, parents and links. */
function wireIds(all: readonly FakeSpan[]): FakeSpan[] {
  const ids = new Map(
    all.map(span => [span.id, `${span.type === 'flow' ? 'flow' : 'op'}:${span.id}`]),
  );
  const wire = (id: string): string => ids.get(id) ?? id;
  return all.map(span => ({
    ...span,
    id: wire(span.id),
    parent: span.parent === null ? null : wire(span.parent),
    ...(span.label === undefined
      ? {}
      : {
          label: {
            ...span.label,
            ...(span.label.links === undefined
              ? {}
              : {links: span.label.links.map(link => ({...link, to: wire(link.to)}))}),
          },
        }),
  }));
}

/** The fake server's seed for run `runId`: the populated batch and snapshot. */
export function prototypeRun(runId: string): FakeServerSeed {
  const all = wireIds(spans());
  const events = all.map((span, index) => ({
    timestamp: '2026-10-15T16:30:51Z',
    type: 'span_started',
    sequence: index + 2,
    data: {kind: 'span_started', span},
  }));
  const last = all.length + 1;
  return {
    runId,
    batch: {
      type: 'event_batch',
      events: [{timestamp: '2026-10-15T16:30:51Z', type: 'server_ready', sequence: 1}, ...events],
      through_sequence: last,
      store_id: 'store-1',
      history_after_sequence: 0,
    },
    snapshot: {run_id: runId, sequence: last, status: 'running', spans: all, overview: OVERVIEW},
  };
}
