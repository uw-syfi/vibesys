/**
 * A fake home server for Playwright: it answers the app's `/api/*` calls (plan 2's contract) from
 * in-memory state. [mock] llm-serve with one saved task (`decode`) and three runs, tokenizer-rs
 * with one run and no tasks. Options switch the states the setup screens show.
 */
import {readFileSync} from 'node:fs';
import type {Page, Route} from '@playwright/test';
import type {
  AuthStatus,
  Catalog,
  FsListing,
  Gateway,
  ProjectValidation,
  RunRow,
  TaskCreate,
  TaskDetail,
  TaskEdit,
  TaskForm,
  TaskSummary,
} from '../src/home-api.js';

const fixture = (name: string): unknown =>
  JSON.parse(readFileSync(new URL(`../src/fixtures/${name}`, import.meta.url), 'utf8'));

export const HOME = '/?token=home';
export const ROOT = '/Users/me/src/llm-serve';
export const OTHER_ROOT = '/Users/me/src/tokenizer-rs';
export const PROJECT_ID = '0123456789abcdef';
const OTHER_ID = 'fedcba9876543210';
export const NEW_RUN = 'llm-serve-20260928-120000';
/** The run e2e/gateway.ts replays; the sidebar lists it as llm-serve's live run. */
export const LIVE_RUN = '20260925-140000-8f21c3a0-web-live';
export const FINISHED_RUN = 'llm-serve-20260923-140000';
/** e2e/gateway.ts answers this socket. */
export const GATEWAY_WS = 'ws://127.0.0.1:5173/ws?token=gw';
const CATALOG = fixture('home-catalog.json') as Catalog;
export const AUTH = fixture('home-auth.json') as AuthStatus;
const STDERR_LOG = '/Users/me/.vibesys/web/gateways/0123456789abcdef/live.stderr';

const DECODE: TaskDetail = {
  name: 'decode',
  objective: 'Increase decode throughput of the batch inference server without changing outputs.',
  domain: 'llm-serving',
  accuracy_command: 'cargo test --release',
  benchmark_command: 'cargo bench --bench decode',
  result: {
    kind: 'metric',
    json_argument: '--json',
    metric: 'median_tok_per_sec',
    protocol_version: null,
  },
  profile_guided: false,
  editable: true,
  read_only_reason: null,
  content_hash: 'h1',
};

/** [mock] The benchmark does not compile: the run server dies measuring the baseline. */
const BASELINE_TAIL = [
  '   Compiling llm-serve v0.1.0 (/Users/me/src/llm-serve)',
  'error[E0599]: no method named `decode_step` found for struct `Batch` in the current scope',
  '  --> benches/decode.rs:41:14',
  'error: could not compile `llm-serve` (bench "decode") due to 1 previous error',
];
/** [mock] The run server refused its configuration before publishing a gateway. */
const LAUNCH_TAIL = [
  'Traceback (most recent call last):',
  '  File "/Users/me/vibesys/src/vibesys/config.py", line 214, in load_config',
  "ValueError: unknown model 'claude-opus-9' for provider claude",
];

type Hold = 'open' | 'attach' | 'key';

export interface HomeOptions {
  validation: Pick<ProjectValidation, 'state' | 'pending'>;
  tasks: TaskSummary[];
  auth: AuthStatus;
  /** POST .../runs: the run attaches, the launch fails, or the run server dies while starting. */
  start: 'live' | 'launch_failed' | 'failed';
  /** PUT /api/auth answers invalid_key. */
  rejectKey: boolean;
  /** POST .../resume answers budget_decrease below this. */
  recordedBudget: number;
  /** POST .../open and .../resume answer unknown_run instead of launching. */
  unknownRun: boolean;
  /** Folders whose validation waits for `release(path)`. */
  slow: string[];
  /** Folder listings (by the requested `path`, `''` for the granted roots) that wait for `releaseFs(path)`. */
  slowFs: string[];
  /** Calls that never answer: `open`, `attach` (a launch stays starting), `key` (a key write). */
  hold: Hold[];
  /** The saved task changes on disk right after it is first read, so an edit of that read conflicts. */
  changedOnDisk: boolean;
  /** Another task file appears between the commit preview and the first commit, which conflicts. */
  commitRace: boolean;
}

export interface FakeHome {
  /** Every API call, in order. */
  requests: {method: string; path: string; body: unknown}[];
  /** Answers the held validation of `path`. */
  release(path: string): void;
  /** Answers the held folder listing of `path` (`''` for the granted roots). */
  releaseFs(path: string): void;
}

const DEFAULTS: HomeOptions = {
  validation: {state: 'ready', pending: []},
  tasks: [{name: 'decode', valid: true, domain: 'llm-serving', error: null}],
  auth: AUTH,
  start: 'live',
  rejectKey: false,
  recordedBudget: 12,
  unknownRun: false,
  slow: [],
  slowFs: [],
  hold: [],
  changedOnDisk: false,
  commitRace: false,
};

interface Reply {
  status: number;
  body: unknown;
}
type Answer = Reply | null | Promise<Reply | null>;
type Handler = (match: RegExpExecArray, body: unknown, search: URLSearchParams) => Answer;

const ok = (body: unknown): Reply => ({status: 200, body});
const fail = (status: number, code: string, message: string, details: unknown = null): Reply => ({
  status,
  body: {error: {code, message, details}},
});
const field = (body: unknown, key: string): unknown =>
  typeof body === 'object' && body !== null ? (body as Record<string, unknown>)[key] : undefined;

function gateway(state: Gateway['state'], patch: Partial<Gateway> = {}): Gateway {
  const serving = state !== 'none' && state !== 'failed';
  return {
    state,
    url: serving ? 'http://127.0.0.1:5173/?token=gw' : null,
    websocket_url: serving ? GATEWAY_WS : null,
    token: serving ? 'gw' : null,
    stderr_tail: [],
    stderr_log: null,
    origin_mismatch: false,
    ...patch,
  };
}

/** A stored run; `objective` null is a launch not yet in the run store (plan 2 leaves the three fields null). */
const run = (
  runId: string,
  status: RunRow['status'],
  state: Gateway['state'],
  rounds: number,
  objective: string | null = null,
  createdAt: string | null = null,
): RunRow => ({
  run_id: runId,
  loop: 'agent',
  status,
  rounds,
  gateway: gateway(state),
  reopen: null,
  error: null,
  task: objective === null ? null : 'decode',
  objective,
  created_at: createdAt,
});

const PROJECT = {id: PROJECT_ID, root: ROOT, name: 'llm-serve'};
const OTHER = {id: OTHER_ID, root: OTHER_ROOT, name: 'tokenizer-rs'};
const PROJECTS = [
  {...PROJECT, last_opened: '2026-09-28T11:00:00Z'},
  {...OTHER, last_opened: '2026-09-21T10:00:00Z'},
];
const RUNS: Readonly<Record<string, RunRow[]>> = {
  [PROJECT_ID]: [
    run(LIVE_RUN, 'active', 'live', 6, 'Increase decode throughput', '2026-09-28T11:59:00+00:00'),
    run(
      FINISHED_RUN,
      'completed',
      'none',
      12,
      'Reduce p99 prefill latency',
      '2026-09-26T12:00:00+00:00',
    ),
    run(
      'llm-serve-20260920-090000',
      'failed',
      'none',
      3,
      'Shrink KV cache footprint',
      '2026-09-23T12:00:00+00:00',
    ),
  ],
  [OTHER_ID]: [
    run(
      'tokenizer-rs-20260921-100000',
      'completed',
      'none',
      8,
      'Speed up the BPE merge loop',
      '2026-09-21T12:00:00+00:00',
    ),
  ],
};
const FOLDERS: Readonly<Record<string, FsListing>> = {
  '': {path: null, parent: null, entries: [{name: 'me', path: '/Users/me', git: false}]},
  '/Users/me': {
    path: '/Users/me',
    parent: null,
    entries: [{name: 'src', path: '/Users/me/src', git: false}],
  },
  '/Users/me/src': {
    path: '/Users/me/src',
    parent: '/Users/me',
    entries: [
      {name: 'llm-serve', path: ROOT, git: true},
      {name: 'tokenizer-rs', path: OTHER_ROOT, git: true},
    ],
  },
};

function listing(path: string | null): FsListing {
  const known = FOLDERS[path ?? ''];
  if (known !== undefined || path === null) return known ?? {path: null, parent: null, entries: []};
  return {path, parent: path.slice(0, path.lastIndexOf('/')) || null, entries: []};
}

const isTaskFile = (path: string) => path.startsWith('.vibesys/tasks/');

class FakeServer {
  readonly requests: FakeHome['requests'] = [];
  private validation: HomeOptions['validation'];
  private readonly tasks: TaskSummary[];
  private readonly details = new Map<string, TaskDetail>([[DECODE.name, DECODE]]);
  private readonly auth: AuthStatus;
  private launched: string | null = null;
  private polls = 0;
  private raced = false;
  private readonly waiting = new Map<string, () => void>();

  constructor(private readonly options: HomeOptions) {
    this.validation = options.validation;
    this.tasks = [...options.tasks];
    this.auth = structuredClone(options.auth);
  }

  release(path: string): void {
    this.waiting.get(path)?.();
    this.waiting.delete(path);
  }

  releaseFs(path: string): void {
    const key = `fs:${path}`;
    this.waiting.get(key)?.();
    this.waiting.delete(key);
  }

  async answer(route: Route): Promise<void> {
    const request = route.request();
    const url = new URL(request.url());
    const body: unknown = request.postData() === null ? null : request.postDataJSON();
    this.requests.push({method: request.method(), path: `${url.pathname}${url.search}`, body});
    const reply = await this.reply(
      `${request.method()} ${decodeURIComponent(url.pathname)}`,
      body,
      url.searchParams,
    );
    if (reply === null) return;
    await route.fulfill({
      status: reply.status,
      contentType: 'application/json',
      body: JSON.stringify(reply.body),
    });
  }

  private reply(route: string, body: unknown, search: URLSearchParams): Answer {
    for (const [pattern, handle] of this.routes()) {
      const match = pattern.exec(route);
      if (match !== null) return handle(match, body, search);
    }
    return fail(404, 'not_found', 'not found');
  }

  private routes(): [RegExp, Handler][] {
    const at = (method: string, suffix: string) =>
      new RegExp(`^${method} /api/projects/([^/]+)${suffix}$`);
    return [
      [/^GET \/api\/projects$/, () => ok({projects: PROJECTS})],
      [/^GET \/api\/fs$/, (_match, _body, search) => this.fs(search.get('path'))],
      [
        /^POST \/api\/projects\/validate$/,
        (_match, body) => this.validate(String(field(body, 'path') ?? '')),
      ],
      [/^GET \/api\/agents\/catalog$/, () => ok(CATALOG)],
      [/^GET \/api\/auth$/, () => ok(this.auth)],
      [/^PUT \/api\/auth\/([^/]+)$/, (match, body) => this.saveKey(match[1] ?? '', body)],
      [at('GET', '/tasks'), match => ok({tasks: match[1] === PROJECT_ID ? this.tasks : []})],
      [at('GET', '/tasks/([^/]+)'), match => this.task(match[2] ?? '')],
      [at('POST', '/tasks'), (_match, body) => this.createTask(body as TaskCreate)],
      [
        at('PUT', '/tasks/([^/]+)'),
        (match, body) => this.editTask(match[2] ?? '', body as TaskEdit),
      ],
      [at('GET', '/commit'), () => ok(this.preview())],
      [at('POST', '/commit'), () => this.commit()],
      [at('GET', '/runs'), match => ok({runs: this.runs(match[1] ?? '')})],
      [at('POST', '/runs'), () => this.start()],
      [at('POST', '/runs/([^/]+)/open'), match => this.open(match[2] ?? '')],
      [
        at('POST', '/runs/([^/]+)/resume'),
        (match, body) => this.resume(match[2] ?? '', field(body, 'budget')),
      ],
    ];
  }

  private validate(path: string): Answer {
    const answer = (): Reply => ok(this.validationOf(path));
    if (!this.options.slow.includes(path)) return answer();
    return new Promise(resolve => this.waiting.set(path, () => resolve(answer())));
  }

  private fs(path: string | null): Answer {
    const key = path ?? '';
    const answer = (): Reply => ok(listing(path));
    if (!this.options.slowFs.includes(key)) return answer();
    return new Promise(resolve => this.waiting.set(`fs:${key}`, () => resolve(answer())));
  }

  private validationOf(path: string): ProjectValidation {
    if (path === ROOT) {
      const tasks = this.tasks.map(task => task.name);
      return {...this.validation, path, project: PROJECT, message: null, tasks};
    }
    if (path === OTHER_ROOT) {
      return {state: 'no_tasks', path, project: OTHER, message: null, tasks: [], pending: []};
    }
    return {state: 'not_git', path, project: null, message: null, tasks: [], pending: []};
  }

  private saveKey(provider: string, body: unknown): Reply | null {
    if (this.options.hold.includes('key')) return null;
    if (this.options.rejectKey) {
      return fail(
        400,
        'invalid_key',
        'The key has a quote, backslash or $; paste it without them.',
      );
    }
    const row = this.auth.providers.find(item => item.provider === provider);
    const key = row?.keys.find(item => item.name === field(body, 'name'));
    if (row === undefined || key === undefined)
      return fail(404, 'unknown_provider', `unknown provider ${provider}`);
    row.status = 'key';
    key.source = 'dotenv';
    return ok({provider, name: key.name, status: 'unverified', shadowed_by_env: key.shadowed});
  }

  private task(name: string): Reply {
    const detail = this.details.get(name);
    if (this.options.changedOnDisk && detail?.content_hash === DECODE.content_hash) {
      const benchmark_command = 'cargo bench --bench decode --features simd';
      this.details.set(name, {...detail, benchmark_command, content_hash: 'h2'});
    }
    return detail === undefined ? fail(404, 'unknown_task', `no task ${name}`) : ok(detail);
  }

  private saved(name: string, form: TaskForm, hash: string): TaskDetail {
    const detail: TaskDetail = {
      ...DECODE,
      name,
      objective: form.objective,
      domain: form.domain,
      accuracy_command: form.accuracy_command,
      benchmark_command: form.benchmark_command,
      result: {
        kind: 'metric',
        json_argument: form.result_json_argument,
        metric: form.result_metric,
        protocol_version: null,
      },
      content_hash: hash,
    };
    this.details.set(name, detail);
    const files = [
      `.vibesys/tasks/${name}/OBJECTIVE.md`,
      `.vibesys/tasks/${name}/vibesys.input.toml`,
    ];
    this.validation = {
      state: 'dirty_tree',
      pending: [...new Set([...this.validation.pending, ...files])].sort(),
    };
    return detail;
  }

  private createTask(body: TaskCreate): Reply {
    if (this.details.has(body.name))
      return fail(409, 'task_exists', `A task named ${body.name} exists`);
    this.tasks.push({name: body.name, valid: true, domain: body.domain, error: null});
    return ok(this.saved(body.name, body, 'h-new'));
  }

  private editTask(name: string, body: TaskEdit): Reply {
    const current = this.details.get(name);
    if (current === undefined) return fail(404, 'unknown_task', `no task ${name}`);
    if (body.base_hash !== current.content_hash) {
      return fail(409, 'task_conflict', 'The task changed on disk since it was loaded');
    }
    return ok(this.saved(name, body, `${current.content_hash}+`));
  }

  private preview(): {task_files: string[]; other: string[]} {
    const pending = this.validation.pending;
    return {
      task_files: pending.filter(isTaskFile),
      other: pending.filter(path => !isTaskFile(path)),
    };
  }

  private commit(): Reply {
    if (this.options.commitRace && !this.raced) {
      this.raced = true;
      const extra = '.vibesys/tasks/decode/profile.toml';
      this.validation = {...this.validation, pending: [...this.validation.pending, extra].sort()};
      return fail(
        409,
        'task_conflict',
        'the task files changed since the preview; review them again',
      );
    }
    const {task_files, other} = this.preview();
    this.validation =
      other.length > 0 ? {state: 'dirty_tree', pending: other} : {state: 'ready', pending: []};
    return ok({commit: 'c0ffee1', committed: task_files});
  }

  private runs(projectId: string): RunRow[] {
    const rows = (RUNS[projectId] ?? []).filter(row => row.run_id !== this.launched);
    const launched = this.launched;
    return projectId === PROJECT_ID && launched !== null
      ? [this.launchRow(launched), ...rows]
      : rows;
  }

  /** The first poll after a launch sees it starting; the next one live, or failed. */
  private launchRow(runId: string): RunRow {
    this.polls += 1;
    if (this.polls < 2 || this.options.hold.includes('attach'))
      return run(runId, 'active', 'starting', 0);
    if (this.options.start !== 'failed') return run(runId, 'active', 'live', 0);
    return {
      ...run(runId, 'failed', 'failed', 0),
      gateway: gateway('failed', {stderr_tail: BASELINE_TAIL, stderr_log: STDERR_LOG}),
    };
  }

  private start(): Reply {
    if (this.options.start === 'launch_failed') {
      return fail(
        502,
        'launch_failed',
        'The run server exited with status 1 before publishing its gateway.',
        {
          stderr_tail: LAUNCH_TAIL,
          stderr_log: STDERR_LOG,
        },
      );
    }
    this.launched = NEW_RUN;
    return ok({run_id: NEW_RUN, gateway: gateway('starting')});
  }

  private open(runId: string): Reply | null {
    if (this.options.unknownRun) return fail(404, 'unknown_run', `no run ${runId}`);
    return this.options.hold.includes('open')
      ? null
      : ok({run_id: runId, gateway: gateway('reopened')});
  }

  private resume(runId: string, budget: unknown): Reply {
    if (this.options.unknownRun) return fail(404, 'unknown_run', `no run ${runId}`);
    if (typeof budget === 'number' && budget < this.options.recordedBudget) {
      return fail(422, 'budget_decrease', 'The budget is below the recorded total', {
        recorded: this.options.recordedBudget,
      });
    }
    this.launched = runId;
    return ok({run_id: runId, gateway: gateway('starting')});
  }
}

export async function mockHome(page: Page, options: Partial<HomeOptions> = {}): Promise<FakeHome> {
  const server = new FakeServer({...DEFAULTS, ...options});
  await page.route(/\/api\//, route => server.answer(route));
  return {
    requests: server.requests,
    release: path => server.release(path),
    releaseFs: path => server.releaseFs(path),
  };
}
