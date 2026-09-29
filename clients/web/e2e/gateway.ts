/**
 * A mocked run gateway for Playwright. It answers the page's `/ws` sockets from the demo
 * recording (src/fixtures/demo-run.jsonl), the run `vibesys web live --demo` replays.
 * Experiments, design and performance are derived from the recording's own plan, implementer and
 * round events. Values the recording does not carry are marked [mock].
 */
import {readFileSync} from 'node:fs';
import type {Page, WebSocketRoute} from '@playwright/test';
import type {
  DesignPatch,
  DesignRound,
  HypothesisEntry,
  HypothesisRound,
  PerformanceContext,
  PerformanceRound,
  RunEvent,
  RunStatus,
} from '@vibesys/backend-client';

type Result = Readonly<Record<string, unknown>>;
type Outcome = NonNullable<HypothesisRound['hypothesis_outcome']>;

/** The protocol's closed set of hypothesis outcomes: anything else in a report is dropped. */
const OUTCOMES: readonly Outcome[] = [
  'continue',
  'supported',
  'nominated',
  'disproven',
  'implementation_failed',
  'inconclusive',
  'blocked',
  'proven',
  'rejected',
  'unmeasured',
];
const isOutcome = (value: string | null): value is Outcome =>
  OUTCOMES.some(outcome => outcome === value);

const RECORDED: RunEvent[] = readFileSync(
  new URL('../src/fixtures/demo-run.jsonl', import.meta.url),
  'utf8',
)
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent);

/** [mock] The recording carries no prompt or todo text. Round 6's implementer, the current turn
 * most screens open on, gets both so the Prompt and Todos disclosures have something to show. */
const MOCK_PROMPT_EXECUTION_ID = '2b7120fc0f905b626a19ed1eae1a6514';
const MOCK_USER_PROMPT =
  'Round 6 plan: mutex contention profiling showed time in the queue mutex under concurrent ' +
  'load. Switch the request queue to a lock-free MPMC ring buffer and confirm throughput does ' +
  'not regress.';
const MOCK_TODOS: {content: string; status: string}[] = [
  {content: 'Profile queue contention under concurrent load', status: 'completed'},
  {content: 'Replace the mutex-backed queue with a lock-free MPMC ring', status: 'completed'},
  {content: 'Run the test suite', status: 'in_progress'},
  {content: 'Benchmark decode throughput', status: 'pending'},
];

/** Gives round 6's implementer a prompt and inserts a todo_update event right after it starts,
 * then renumbers sequences (the recording's are contiguous from 1) so both stay consistent. */
function withMockPromptAndTodos(events: readonly RunEvent[]): RunEvent[] {
  const withTodos = events.flatMap(event => {
    if (
      event.execution_id !== MOCK_PROMPT_EXECUTION_ID ||
      event.data?.kind !== 'agent_execution_started'
    ) {
      return [event];
    }
    const started: RunEvent = {...event, data: {...event.data, user_prompt: MOCK_USER_PROMPT}};
    const todoUpdate: RunEvent = {
      ...event,
      type: 'todo_update',
      data: {kind: 'todo_update', todos: MOCK_TODOS},
    };
    return [started, todoUpdate];
  });
  return withTodos.map((event, index) => ({...event, sequence: index + 1}));
}

export const DEMO: RunEvent[] = withMockPromptAndTodos(RECORDED);
/** Round 6's judge is working: its execution started (232, 233) and has not finished (234), one
 * later than the recording's own sequence because the mock todo_update above adds an event. */
export const LIVE_THROUGH = 233;
export const FINISHED = DEMO.at(-1)?.sequence ?? 0;
/** [mock] The objective and the 950 tok/s baseline the recording's round 1 judge states. */
export const DEMO_CONTEXT: PerformanceContext = {
  objective_metric: 'median_tok_per_sec',
  objective_unit: 'tok/s',
  objective_direction: 'max',
  objective_baseline_value: 950,
  objective_description:
    'Increase decode throughput of the batch inference server without changing outputs.',
};

const roundOf = (label: string | null | undefined): number =>
  Number(/round-(\d+)/.exec(label ?? '')?.[1] ?? 0);
/** [mock] The recording keeps no commits; each round gets a stable fake id. */
export const commitOf = (round: number): string => `c0ffee${String(round).padStart(2, '0')}`;
const text = (result: Result | null, key: string): string | null => {
  const value = result?.[key];
  return typeof value === 'string' ? value : null;
};

function finished(events: readonly RunEvent[], label: RegExp): Result | null {
  for (const event of [...events].reverse()) {
    const data = event.data;
    if (data?.kind === 'agent_execution_finished' && label.test(event.round_label ?? '')) {
      return data.result ?? null;
    }
  }
  return null;
}

interface RoundEnd {
  verdict: 'pass' | 'fail' | 'skipped';
  perf: number | null;
  unit: string | null;
}

function roundEnds(events: readonly RunEvent[]): Map<number, RoundEnd> {
  const ends = new Map<number, RoundEnd>();
  for (const event of events) {
    const data = event.data;
    if (data?.kind !== 'round_finished') continue;
    ends.set(roundOf(event.round_label), {
      verdict: data.judge_verdict,
      perf: data.perf_metric ?? null,
      unit: data.perf_unit ?? null,
    });
  }
  return ends;
}

function roundFact(events: readonly RunEvent[], round: number, end: RoundEnd): HypothesisRound {
  const report = finished(events, new RegExp(`^round-${round}-retry-\\d+-implementer$`));
  const outcome = text(report, 'hypothesis_outcome');
  return {
    round,
    passed: true,
    reviewed: true,
    judge_verdict: end.verdict === 'skipped' ? null : end.verdict,
    perf_metric: end.perf,
    perf_unit: end.unit,
    commit: commitOf(round),
    ...(isOutcome(outcome) ? {hypothesis_outcome: outcome} : {}),
  };
}

export function demoExperiments(events: readonly RunEvent[]): HypothesisEntry[] {
  const ends = roundEnds(events);
  return events.flatMap(event => {
    const data = event.data;
    if (
      data?.kind !== 'agent_execution_finished' ||
      !/^round-\d+-plan$/.test(event.round_label ?? '')
    ) {
      return [];
    }
    const round = roundOf(event.round_label);
    const plan: Result = data.result ?? {};
    const end = ends.get(round);
    return [
      {
        hypothesis_id: text(plan, 'hypothesis_id') ?? `H-${round}`,
        title: text(plan, 'title') ?? '',
        claim: text(plan, 'hypothesis') ?? '',
        first_round: round,
        last_round: round,
        rounds: end === undefined ? [] : [roundFact(events, round, end)],
        active: end === undefined,
      },
    ];
  });
}

interface Edit {
  path: string;
  tool: string;
  before: string;
  after: string;
}

function editsOf(events: readonly RunEvent[], round: number): Edit[] {
  return events.flatMap(event => {
    const data = event.data;
    if (data?.kind !== 'tool_call' || roundOf(event.round_label) !== round) return [];
    const args = data.args ?? {};
    const path = args['file_path'];
    if (typeof path !== 'string' || (data.tool !== 'Edit' && data.tool !== 'Write')) return [];
    const before = args['old_string'];
    const after = args['new_string'] ?? args['content'];
    return [
      {
        path,
        tool: data.tool,
        before: typeof before === 'string' ? before : '',
        after: typeof after === 'string' ? after : '',
      },
    ];
  });
}

export function demoDesign(events: readonly RunEvent[]): DesignRound[] {
  return [...roundEnds(events).keys()].map(round => ({
    round,
    base: commitOf(round - 1),
    commit: commitOf(round),
    files: [...new Set(editsOf(events, round).map(edit => edit.path))].map(path => ({
      path,
      change: 'modified' as const,
    })),
  }));
}

/**
 * Edits become a patch. A file the implementer wrote whole has no prior text in the recording,
 * so the workspace "cannot produce" it (patch null); round 3's patches are marked truncated.
 */
export function demoPatch(
  events: readonly RunEvent[],
  base: string,
  head: string,
  path: string,
): DesignPatch {
  const round = Number(head.slice(-2));
  const edits = editsOf(events, round).filter(edit => edit.path === path);
  if (edits.length === 0 || edits.some(edit => edit.tool === 'Write')) {
    return {base, head, path, patch: null, truncated: false};
  }
  const hunks = edits.map(edit => {
    const before = edit.before.split('\n');
    const after = edit.after.split('\n');
    return [
      `@@ -1,${before.length} +1,${after.length} @@`,
      ...before.map(line => `-${line}`),
      ...after.map(line => `+${line}`),
    ].join('\n');
  });
  const patch = [`diff --git a/${path} b/${path}`, `--- a/${path}`, `+++ b/${path}`, ...hunks, ''];
  return {base, head, path, patch: patch.join('\n'), truncated: round === 3};
}

function demoPerformance(events: readonly RunEvent[]): PerformanceRound[] {
  return [...roundEnds(events)].flatMap(([round, end]) =>
    end.perf === null
      ? []
      : [{round, perf_metric: end.perf, perf_unit: end.unit ?? '', passed: end.verdict === 'pass'}],
  );
}

export interface GatewayRequest {
  type: string;
  request_id?: string;
  after_sequence?: number;
  before_sequence?: number | null;
  base?: string;
  head?: string;
  path?: string;
  text?: string;
}

export interface Gateway {
  /** Appends an event to the run and sends it to every open stream. */
  push(event: Partial<RunEvent> & Pick<RunEvent, 'type'>): void;
  setStatus(status: RunStatus): void;
  requests: GatewayRequest[];
}

function answer(
  request: GatewayRequest,
  events: readonly RunEvent[],
  snapshot: () => object,
): Record<string, unknown> {
  switch (request.type) {
    case 'query.snapshot':
      return {snapshot: snapshot()};
    case 'query.experiments':
      return {experiments_ready: true, experiments: demoExperiments(events)};
    case 'query.design':
      return {design_ready: true, design: demoDesign(events)};
    case 'query.performance':
      return {performance: demoPerformance(events), performance_context: DEMO_CONTEXT};
    case 'query.design_patch':
      return {
        design_patch: demoPatch(events, request.base ?? '', request.head ?? '', request.path ?? ''),
      };
    case 'query.events':
      return {
        events: events.filter(event => {
          const sequence = event.sequence ?? 0;
          const before = request.before_sequence;
          return sequence > (request.after_sequence ?? 0) && (before == null || sequence < before);
        }),
      };
    default:
      return request.type.startsWith('command.')
        ? {ack: {action: request.type.slice('command.'.length), status: 'pending'}}
        : {};
  }
}

/** The run the mock serves: its events so far, its status, and the open streams. */
class DemoRun {
  readonly events: RunEvent[];
  readonly runId = DEMO[0]?.run_id ?? 'demo';
  readonly streams = new Set<WebSocketRoute>();
  readonly requests: GatewayRequest[] = [];
  sequence: number;
  status: RunStatus;

  constructor(through: number, status: RunStatus | undefined) {
    this.events = DEMO.filter(event => (event.sequence ?? 0) <= through);
    this.sequence = this.events.at(-1)?.sequence ?? 0;
    this.status = status ?? (through >= FINISHED ? 'completed' : 'running');
  }

  push: Gateway['push'] = partial => {
    this.sequence += 1;
    const event = {
      run_id: this.runId,
      timestamp: new Date(Date.UTC(2026, 8, 25, 14, 2) + this.sequence * 1000).toISOString(),
      ...partial,
      sequence: this.sequence,
    } as RunEvent;
    this.events.push(event);
    for (const ws of this.streams) ws.send(JSON.stringify({type: 'event', event}));
  };

  setStatus = (next: RunStatus): void => {
    const previous = this.status;
    this.status = next;
    this.push({
      type: 'run_status_changed',
      data: {kind: 'run_status_changed', status: next, previous},
    });
  };

  /** What the running backend does after acknowledging a command. */
  react(request: GatewayRequest): void {
    if (request.type === 'command.pause') this.setStatus('pausing');
    if (request.type === 'command.resume') this.setStatus('running');
    if (request.type === 'command.stop') this.setStatus('stopping');
    if (request.type === 'command.steer') {
      const text = `/steer: ${request.text ?? ''}`;
      const scope = {agent_kind: 'judge', round_label: 'round-6-retry-1-judge'};
      this.push({type: 'control', status: 'pending', text, ...scope});
    }
  }

  onRequest(ws: WebSocketRoute, request: GatewayRequest): void {
    this.requests.push(request);
    const snapshot = () => ({run_id: this.runId, sequence: this.sequence, status: this.status});
    const fields = answer(request, this.events, snapshot);
    ws.send(
      JSON.stringify({protocol_version: 1, request_id: request.request_id, ok: true, ...fields}),
    );
    this.react(request);
  }

  subscribe(ws: WebSocketRoute, request: GatewayRequest): void {
    this.streams.add(ws);
    const after = request.after_sequence ?? 0;
    ws.send(
      JSON.stringify({
        type: 'subscribed',
        request_id: request.request_id,
        run_id: this.runId,
        latest_sequence: this.sequence,
      }),
    );
    ws.send(
      JSON.stringify({
        type: 'event_batch',
        events: this.events.filter(event => (event.sequence ?? 0) > after),
        through_sequence: this.sequence,
        active_executions: [],
        history_after_sequence: 0,
      }),
    );
  }
}

export async function mockGateway(
  page: Page,
  options: {through?: number; status?: RunStatus} = {},
): Promise<Gateway> {
  const run = new DemoRun(options.through ?? LIVE_THROUGH, options.status);
  await page.routeWebSocket(/\/ws(\?|$)/, ws => {
    ws.onMessage(raw => {
      const request = JSON.parse(String(raw)) as GatewayRequest;
      if (request.type === 'subscribe') run.subscribe(ws, request);
      else run.onRequest(ws, request);
    });
  });
  return {push: run.push, setStatus: run.setStatus, requests: run.requests};
}
