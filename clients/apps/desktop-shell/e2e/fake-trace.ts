/**
 * The fake run's trace: the span events a dynamic run on queue-rs writes over two planning calls,
 * as the server sends them (`span_started`, `span_updated`, `span_ended`, each with the whole
 * span). The run's root, the input's measurement, two planning calls, seven workstreams (some
 * building on earlier ones, finished ones accepted or rejected), their agent turns and gates, and
 * one span of a kind no client knows. Times are run-clock seconds from the run's start.
 */

type Status = 'pending' | 'running' | 'waiting' | 'done' | 'failed' | 'stopped';

interface FakeSpan {
  readonly id: string;
  readonly parent: string | null;
  readonly type: 'flow' | 'operation';
  readonly name: string;
  readonly status: Status;
  readonly started_at: number;
  readonly ended_at?: number | null;
  readonly waiting_on?: readonly {kind: string; ref: string; since: number}[];
  readonly attempts?: readonly Record<string, unknown>[];
  readonly failure?: {kind: string; message: string} | null;
  readonly label?: {
    kind: string;
    title?: string;
    attrs_json?: string;
    links?: readonly {rel: string; to: string}[];
    verdict?: string;
    reason?: string;
  } | null;
}

export const RUN_ID = '20261015-163047';
const START = Date.UTC(2026, 9, 15, 16, 30, 51);

const ROOT = 'flow:root#1';
export const W5_GATE = 'op:w5-gate';

function workstream(
  n: number,
  title: string,
  at: number,
  builds: readonly number[] = [],
): FakeSpan {
  return {
    id: `flow:root/ws-${n}#1`,
    parent: ROOT,
    type: 'flow',
    name: 'workstream',
    status: 'running',
    started_at: at,
    label: {
      kind: 'workstream',
      title,
      attrs_json: JSON.stringify({
        hypothesis_id: `W${n}`,
        parent_revision: builds.length > 0 ? `r${builds[0]}` : 'r0',
      }),
      links: builds.map(to => ({rel: 'builds_on', to: `flow:root/ws-${to}#1`})),
    },
  };
}

function turn(id: string, parent: string, role: string, at: number): FakeSpan {
  return {
    id,
    parent,
    type: 'operation',
    name: 'agent.turn',
    status: 'running',
    started_at: at,
    label: {
      kind: 'agent_turn',
      title: role,
      attrs_json: JSON.stringify({role, model: 'claude-sonnet-5'}),
    },
  };
}

function gate(id: string, parent: string, at: number): FakeSpan {
  return {
    id,
    parent,
    type: 'operation',
    name: 'evaluation.measure',
    status: 'running',
    started_at: at,
    label: {
      kind: 'gate',
      title: 'correctness + benchmark',
      attrs_json: JSON.stringify({stage: 'correctness,benchmark'}),
    },
  };
}

function planning(n: number, at: number): FakeSpan {
  return {
    id: `flow:root/plan#${n}`,
    parent: ROOT,
    type: 'flow',
    name: 'plan',
    status: 'running',
    started_at: at,
    label: {
      kind: 'planning_call',
      title: `planning call ${n}`,
      attrs_json: JSON.stringify({call: n}),
    },
  };
}

/** A span that ended at `at` with `status`, and optionally a verdict, measurement or failure. */
function ended(
  span: FakeSpan,
  at: number,
  status: Status,
  extra: Partial<FakeSpan> = {},
): FakeSpan {
  return {...span, status, ended_at: at, waiting_on: [], ...extra};
}

function verdict(
  span: FakeSpan,
  value: string,
  reason?: string,
  attrs?: Record<string, unknown>,
): NonNullable<FakeSpan['label']> {
  return {
    ...(span.label ?? {kind: 'unknown'}),
    verdict: value,
    ...(reason === undefined ? {} : {reason}),
    ...(attrs === undefined ? {} : {attrs_json: JSON.stringify(attrs)}),
  };
}

type Step = readonly [string, FakeSpan];

/** Try 1 of 3 failed at `failedAt` for `reason`; try 2 runs, or ended at `doneAt`. */
function retried(start: number, failedAt: number, reason: string, doneAt?: number) {
  return [
    {n: 1, of: 3, started_at: start, ended_at: failedAt, outcome: 'again', reason},
    {
      n: 2,
      of: 3,
      started_at: failedAt + 1,
      ...(doneAt === undefined ? {} : {ended_at: doneAt, outcome: 'done'}),
    },
  ];
}

/** The input's measurement and the first planning call: four workstreams, all finished. */
function firstCall(): readonly Step[] {
  const input: FakeSpan = {
    id: 'flow:root/input#1',
    parent: ROOT,
    type: 'flow',
    name: 'measure the input',
    status: 'running',
    started_at: 330,
  };
  const plan = planning(1, 374);
  const planner = turn('op:plan1-turn', plan.id, 'planner', 375);
  const titles = [
    'Lock-free ring with per-slot sequence numbers',
    'Cache-line padding and batched enqueue',
    'Spin then park idle consumers',
    'Larger batch size',
  ];
  const [w1, w2, w3, w4] = titles.map((title, index) => workstream(index + 1, title, 410 + index));
  if (w1 === undefined || w2 === undefined || w3 === undefined || w4 === undefined) return [];
  const w1Turn = turn('op:w1-turn', w1.id, 'implementer', 412);
  const w1Gate = gate('op:w1-gate', w1.id, 900);
  const borrow = 'build failed: borrow error in lib.rs';
  const measured = {stage: 'correctness,benchmark', metric_value: 48.6, vs_baseline: 1.18};
  return [
    ['span_started', input],
    ['span_ended', ended(input, 370, 'done')],
    ['span_started', plan],
    ['span_started', planner],
    ['span_ended', ended(planner, 405, 'done')],
    ['span_ended', ended(plan, 409, 'done')],
    ...[w1, w2, w3, w4].map(each => ['span_started', each] as const),
    ['span_started', w1Turn],
    ['span_updated', {...w1Turn, attempts: retried(412, 640, borrow)}],
    ['span_ended', ended(w1Turn, 890, 'done', {attempts: retried(412, 640, borrow, 890)})],
    ['span_started', w1Gate],
    [
      'span_ended',
      ended(w1Gate, 1100, 'done', {label: verdict(w1Gate, 'passed', undefined, measured)}),
    ],
    ['span_ended', ended(w1, 1101, 'done', {label: verdict(w1, 'accepted')})],
    ['span_ended', ended(w2, 1150, 'done', {label: verdict(w2, 'accepted')})],
    ['span_ended', ended(w3, 1180, 'done', {label: verdict(w3, 'rejected', 'slower_than_input')})],
    [
      'span_ended',
      ended(w4, 1200, 'failed', {
        label: verdict(w4, 'rejected', 'failed_check'),
        failure: {kind: 'outcome', message: 'the correctness check failed'},
      }),
    ],
  ];
}

/** The second planning call: three workstreams building on earlier ones, still at work. */
function secondCall(): readonly Step[] {
  const plan = planning(2, 1211);
  const planner = turn('op:plan2-turn', plan.id, 'planner', 1212);
  const w5 = workstream(5, 'Combine W1 ring with W2 padding', 1239, [1]);
  const w6 = workstream(6, 'Wait-free consumer path', 1240, [1]);
  const w7: FakeSpan = {
    ...workstream(7, 'Per-core queues with stealing', 1241, [5]),
    status: 'waiting',
    waiting_on: [{kind: 'slot', ref: 'group', since: 1241}],
  };
  const w5Turn = turn('op:w5-turn', w5.id, 'implementer', 1240);
  const w6Turn = turn('op:w6-turn', w6.id, 'implementer', 1241);
  const w5Judge = turn('op:w5-judge', w5.id, 'judge', 1640);
  const probe: FakeSpan = {
    id: 'op:cache-warmup',
    parent: ROOT,
    type: 'operation',
    name: 'cache.warm',
    status: 'done',
    started_at: 1250,
    ended_at: 1262,
    label: {
      kind: 'cache_warmup',
      title: 'Warm the build cache',
      attrs_json: JSON.stringify({entries: 412}),
    },
  };
  return [
    ['span_started', plan],
    ['span_started', planner],
    ['span_ended', ended(planner, 1236, 'done')],
    ['span_ended', ended(plan, 1238, 'done')],
    ['span_started', w5],
    ['span_started', w6],
    ['span_started', w7],
    ['span_started', w5Turn],
    ['span_started', w6Turn],
    ['span_started', probe],
    ['span_ended', ended(w5Turn, 1630, 'done')],
    ['span_started', gate(W5_GATE, w5.id, 1631)],
    ['span_started', w5Judge],
    ['span_ended', ended(w5Judge, 1700, 'done')],
    [
      'span_updated',
      {...w6Turn, attempts: retried(1241, 1500, 'build failed: missing Send bound')},
    ],
  ];
}

/** The run's span events in order: `[type, span]` pairs. */
function story(): readonly Step[] {
  const root: FakeSpan = {
    id: ROOT,
    parent: null,
    type: 'flow',
    name: 'dynamic',
    status: 'running',
    started_at: 0,
  };
  return [['span_started', root], ...firstCall(), ...secondCall()];
}

/** The events as the server sends them, numbered from 1, a few seconds apart on the wall clock. */
export function traceEvents(): readonly Record<string, unknown>[] {
  let clock = 0;
  return story().map(([type, span], index) => {
    const tries = (span.attempts ?? []).map(attempt => Number(attempt['started_at']));
    clock = Math.max(clock, span.ended_at ?? 0, span.started_at, ...tries);
    const stamp = new Date(START + clock * 1000).toISOString().slice(0, 19);
    return {
      protocol_version: 1,
      run_id: RUN_ID,
      sequence: index + 1,
      timestamp: `${stamp}+00:00`,
      type,
      data: {kind: type, span},
    };
  });
}

/**
 * W5's agent turn as the server sends it after the span events: reasoning, a reply, and a tool call
 * with its result, each stamped with the turn's span id. `after` is the last span event's sequence.
 */
export function turnEvents(after: number): readonly Record<string, unknown>[] {
  const stamp = new Date(START + 1700 * 1000).toISOString().slice(0, 19);
  const bodies: readonly (readonly [string, Record<string, unknown>])[] = [
    [
      'agent_output_chunk',
      {
        kind: 'agent_output_chunk',
        channel: 'analysis',
        content: 'The head and tail counters share a cache line. ',
      },
    ],
    [
      'agent_output_chunk',
      {
        kind: 'agent_output_chunk',
        channel: 'assistant',
        content: 'I will pad each counter to its own line and re-measure.',
      },
    ],
    [
      'tool_call',
      {
        kind: 'tool_call',
        tool: 'Bash',
        call_id: 'c1',
        args: {command: 'cargo bench --bench queue'},
      },
    ],
    [
      'tool_result',
      {
        kind: 'tool_result',
        tool: 'Bash',
        call_id: 'c1',
        content: 'queue/mpmc time: 41.2 ms',
        is_error: false,
      },
    ],
  ];
  return bodies.map(([type, data], index) => ({
    protocol_version: 1,
    run_id: RUN_ID,
    sequence: after + index + 1,
    timestamp: `${stamp}+00:00`,
    type,
    invocation_id: 'inv-w5',
    agent_kind: 'implementer',
    span_id: 'op:w5-turn',
    data,
  }));
}

const JUDGE_REPLY = {
  analysis:
    'The current src/lib.rs matches the declared outcome: the counters sit on separate cache lines and the correctness gate passed.',
  feedback: '',
  verdict: 'pass',
  skills_used: [],
};

const JUDGE_SCHEMA = {
  $defs: {Verdict: {enum: ['pass', 'fail'], type: 'string'}},
  properties: {
    analysis: {type: 'string'},
    feedback: {type: 'string'},
    verdict: {$ref: '#/$defs/Verdict'},
    skills_used: {type: 'array', items: {type: 'object'}},
  },
  required: ['analysis', 'feedback', 'verdict'],
  type: 'object',
};

/**
 * The judge's turn under W5: the raw JSON it streamed, then the finish event that carries the
 * same reply as typed data, as the server sends them. `after` is the last event's sequence.
 */
export function judgeEvents(after: number): readonly Record<string, unknown>[] {
  const stamp = new Date(START + 1750 * 1000).toISOString().slice(0, 19);
  const raw = JSON.stringify({reply: JUDGE_REPLY});
  const common = {
    protocol_version: 1,
    run_id: RUN_ID,
    timestamp: `${stamp}+00:00`,
    invocation_id: 'inv-w5-judge',
    agent_kind: 'judge',
    span_id: 'op:w5-judge',
  };
  return [
    {
      ...common,
      sequence: after + 1,
      type: 'agent_output_chunk',
      data: {kind: 'agent_output_chunk', channel: 'assistant', content: raw},
    },
    {
      ...common,
      sequence: after + 2,
      type: 'agent_execution_finished',
      status: 'completed',
      data: {
        kind: 'agent_execution_finished',
        result: raw,
        reply: {kind: 'JudgeResponse', json_schema: JUDGE_SCHEMA, value: JUDGE_REPLY},
      },
    },
  ];
}
