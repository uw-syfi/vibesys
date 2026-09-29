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
/** Round 6's judge has finished and the round with it: where a pause requested during the judge
 * call takes effect. */
export const ROUND_6_FINISHED =
  DEMO.find(event => event.type === 'round_finished' && event.round_label === 'round-6')
    ?.sequence ?? 0;
/** [mock] The objective and the 950 tok/s baseline the recording's round 1 judge states. */
export const DEMO_CONTEXT: PerformanceContext = {
  objective_metric: 'median_tok_per_sec',
  objective_unit: 'tok/s',
  objective_direction: 'max',
  objective_baseline_value: 950,
  objective_description:
    'Increase decode throughput of the batch inference server without changing outputs.',
};

/** [mock] The run's chat offer: the run model, then one suggestion. */
const CHAT_OPTIONS = {
  providers: [
    {
      provider: 'claude',
      models: [
        {model: 'claude-opus-5', source: 'run', default: true},
        {model: 'claude-sonnet-5', source: 'suggested'},
      ],
    },
  ],
};
/** [mock] The recording has no chat agent behind it. */
export const MOCK_ANSWER =
  'Mock reply. This gateway replays a recorded run; there is no agent behind it.';
/**
 * `late` offers nothing on the first ask and `error` fails it; both offer after that. `held`
 * offers chat but never answers a question; `unanswered` never answers the options query.
 */
type ChatMode = 'on' | 'off' | 'late' | 'error' | 'held' | 'unanswered';

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
  /** The file line the edit starts at: the latest search hit on the file before it, else 1. */
  start: number;
}

/** `src/sampler.rs:104:…`, the first line of a search result that names `path`. */
function hitLine(event: RunEvent, path: string): number | null {
  const data = event.data;
  if (data?.kind !== 'tool_result') return null;
  const output = data.content || (data.payload?.kind === 'command' ? data.payload.stdout : '');
  const escaped = path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const line = new RegExp(`^${escaped}:(\\d+):`).exec(output ?? '')?.[1];
  return line === undefined ? null : Number(line);
}

function startOf(earlier: readonly RunEvent[], round: number, path: string): number {
  for (const event of [...earlier].reverse()) {
    const line = roundOf(event.round_label) === round ? hitLine(event, path) : null;
    if (line !== null) return line;
  }
  return 1;
}

function editsOf(events: readonly RunEvent[], round: number): Edit[] {
  return events.flatMap((event, index) => {
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
        start: startOf(events.slice(0, index), round, path),
      },
    ];
  });
}

/** One edit as a unified hunk: the lines its old and new text share at either end are context. */
function hunkOf(edit: Edit, shift: number): {text: string; shift: number} {
  const old = edit.before.replace(/\n$/, '').split('\n');
  const next = edit.after.replace(/\n$/, '').split('\n');
  let head = 0;
  while (head < old.length && head < next.length && old[head] === next[head]) head += 1;
  let tail = 0;
  while (
    tail < old.length - head &&
    tail < next.length - head &&
    old[old.length - 1 - tail] === next[next.length - 1 - tail]
  ) {
    tail += 1;
  }
  const rows = [
    ...old.slice(0, head).map(line => ` ${line}`),
    ...old.slice(head, old.length - tail).map(line => `-${line}`),
    ...next.slice(head, next.length - tail).map(line => `+${line}`),
    ...old.slice(old.length - tail).map(line => ` ${line}`),
  ];
  const header = `@@ -${edit.start},${old.length} +${edit.start + shift},${next.length} @@`;
  return {text: [header, ...rows].join('\n'), shift: shift + next.length - old.length};
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
 * Edits become a patch, one hunk each, positioned at the search hit before the edit. A file the
 * implementer wrote whole has no prior text in the recording, so the workspace "cannot produce"
 * it (patch null). Round 3's patch is cut after its first hunk and marked truncated.
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
  let shift = 0;
  const hunks = [...edits]
    .sort((left, right) => left.start - right.start)
    .map(edit => {
      const hunk = hunkOf(edit, shift);
      shift = hunk.shift;
      return hunk.text;
    });
  const truncated = round === 3 && hunks.length > 1;
  const kept = truncated ? hunks.slice(0, 1) : hunks;
  const patch = [`diff --git a/${path} b/${path}`, `--- a/${path}`, `+++ b/${path}`, ...kept, ''];
  return {base, head, path, patch: patch.join('\n'), truncated};
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
  thread_id?: string | null;
  provider?: string;
  model?: string;
  title?: string | null;
}

export interface Gateway {
  /** Appends an event to the run and sends it to every open stream. */
  push(event: Partial<RunEvent> & Pick<RunEvent, 'type'>): void;
  setStatus(status: RunStatus): void;
  /** Sends the recording's next events, up to and including sequence `through`. */
  advance(through: number): void;
  requests: GatewayRequest[];
  /** Closes every open stream, as a dropped connection would; the page then reconnects. */
  drop(): void;
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
  /** The last recorded event sent; pushed events renumber, so this is tracked apart. */
  played: number;
  /** Request types answered with an error instead of an acknowledgment. */
  readonly rejects: ReadonlySet<string>;
  threads = 0;
  optionsAsked = 0;
  chat: ChatMode = 'on';

  constructor(through: number, status: RunStatus | undefined, rejects: readonly string[]) {
    this.rejects = new Set(rejects);
    this.played = through;
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

  advance = (through: number): void => {
    for (const event of DEMO) {
      const sequence = event.sequence ?? 0;
      if (sequence > this.played && sequence <= through) this.push(event);
    }
    this.played = Math.max(this.played, through);
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

  /** Experiment chat as the backend answers it: recorded to the journal first, then returned. */
  chatAnswer(request: GatewayRequest): Record<string, unknown> | null {
    switch (request.type) {
      case 'query.chat_options': {
        this.optionsAsked += 1;
        const offered = this.chat !== 'off' && (this.chat !== 'late' || this.optionsAsked > 1);
        return offered ? {chat_options: CHAT_OPTIONS} : {};
      }
      case 'query.chat_thread_create':
        return this.createThread(request);
      case 'query.chat':
        return this.answerChat(request);
      default:
        return null;
    }
  }

  createThread(request: GatewayRequest): Record<string, unknown> {
    const spec = {
      thread_id: `thread-${++this.threads}`,
      title: request.title ?? '',
      driver: 'agentshim',
      provider: request.provider ?? 'claude',
      model: request.model ?? 'claude-opus-5',
    };
    this.push({
      type: 'chat_thread_created',
      agent_kind: 'chat',
      round_label: 'experiment-chat',
      chat_thread_id: spec.thread_id,
      data: {kind: 'chat_thread_created', ...spec, created_at: '2026-09-25T14:02:00Z'},
    });
    return {chat_thread: spec, events: [this.events.at(-1)]};
  }

  answerChat(request: GatewayRequest): Record<string, unknown> {
    const thread = request.thread_id ?? null;
    const first =
      thread !== null &&
      !this.events.some(event => event.type === 'chat' && event.chat_thread_id === thread);
    this.push({
      type: 'chat',
      text: request.text ?? '',
      status: 'answered',
      agent_kind: 'chat',
      round_label: 'experiment-chat',
      chat_thread_id: thread,
      data: {
        kind: 'chat',
        answer: MOCK_ANSWER,
        thread_title: first ? (request.text ?? null) : null,
        invocation_id: `chat-${this.sequence + 1}`,
      },
    });
    return {
      chat: {question: request.text ?? '', answer: MOCK_ANSWER, thread_id: thread},
      events: [this.events.at(-1)],
    };
  }

  drop = (): void => {
    for (const ws of this.streams) void ws.close();
    this.streams.clear();
  };

  onRequest(ws: WebSocketRoute, request: GatewayRequest): void {
    this.requests.push(request);
    const failed =
      this.chat === 'error' && request.type === 'query.chat_options' && this.optionsAsked === 0;
    if (failed) this.optionsAsked += 1;
    if (this.chat === 'held' && request.type === 'query.chat') return;
    if (this.chat === 'unanswered' && request.type === 'query.chat_options') return;
    if (this.rejects.has(request.type) || failed) {
      ws.send(
        JSON.stringify({
          protocol_version: 1,
          request_id: request.request_id,
          ok: false,
          error: 'Command rejected',
          diagnostic: {code: 'illegal_transition', summary: 'The run refused.', scope: 'request'},
        }),
      );
      return;
    }
    const snapshot = () => ({run_id: this.runId, sequence: this.sequence, status: this.status});
    const fields = this.chatAnswer(request) ?? answer(request, this.events, snapshot);
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
  options: {
    through?: number;
    status?: RunStatus;
    reject?: readonly string[];
    chat?: Exclude<ChatMode, 'on'>;
  } = {},
): Promise<Gateway> {
  const run = new DemoRun(options.through ?? LIVE_THROUGH, options.status, options.reject ?? []);
  run.chat = options.chat ?? 'on';
  // main.tsx probes GET /api/projects to tell a home page from a gateway's own page. Without a
  // mocked home server, Vite's dev proxy forwards it to a home server that isn't running, which
  // logs ECONNREFUSED noise for every gateway-only screen; answer 404 so main.tsx falls through
  // to gateway mode instead. Registered before mockHome (called first in tests using both) so
  // mockHome's own, later-registered handler for /api/ wins and answers for real.
  await page.route(/\/api\/projects(\?|$)/, route =>
    route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({error: {code: 'not_found', message: 'not found', details: null}}),
    }),
  );
  await page.routeWebSocket(/\/ws(\?|$)/, ws => {
    ws.onMessage(raw => {
      const request = JSON.parse(String(raw)) as GatewayRequest;
      if (request.type === 'subscribe') run.subscribe(ws, request);
      else run.onRequest(ws, request);
    });
  });
  return {
    push: run.push,
    setStatus: run.setStatus,
    advance: run.advance,
    requests: run.requests,
    drop: run.drop,
  };
}

/** [mock] The home server's notes endpoint (plan 2), last write wins. */
export async function mockNotes(
  page: Page,
  text: string | null,
): Promise<{puts: string[]; auth: string[]}> {
  const seen = {puts: [] as string[], auth: [] as string[]};
  let note = text;
  await page.route('**/api/notes/*', async route => {
    const request = route.request();
    seen.auth.push(request.headers()['authorization'] ?? '');
    const runId = decodeURIComponent(new URL(request.url()).pathname.split('/').at(-1) ?? '');
    if (request.method() === 'PUT') {
      note = (request.postDataJSON() as {text: string}).text;
      seen.puts.push(note);
    }
    const record =
      note === null
        ? null
        : {runId, text: note, createdAt: '2026-09-25T14:00:00Z', updatedAt: '2026-09-25T14:05:00Z'};
    await route.fulfill({json: {note: record}});
  });
  return seen;
}
