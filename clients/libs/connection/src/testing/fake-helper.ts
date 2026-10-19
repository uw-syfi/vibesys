/**
 * `FakeHelper`: an in-memory host helper that speaks the helper protocol on the wire, and
 * `FakeLink`, the `Link` over it. It is the one Fake of a host: every channel is an in-memory
 * process that reads request lines and writes reply lines, parsed and checked against the same
 * generated contract as the real helper's (`parseHelperRequest`), so it is never more forgiving than
 * a real helper: the `helper-*` conformance scenarios replay against it unchanged.
 *
 * The test controls the host: its registry (`publish`, `end`), its projects and directories, how a
 * run answers (`runs`, `denied`), and its faults: a link that refuses or is down (`linkFault`), a
 * bootstrap that fails (`bootstrapExit`) or reports what it found (`preamble`), a helper that never
 * answers (`silent`), answers some requests past their deadline (`expired`) or with a line outside
 * the contract (`garbled`), or exits after its handshake (`exitAfterHello`), and dropping every
 * channel at once (`drop`, a broken link).
 */
import {
  type AgentLogin,
  type Backend,
  type ChannelEndOutcome,
  type Check,
  type CheckoutInspectReply,
  type ClusterTestFailed,
  type ClusterTestPassed,
  type ComputeSettings,
  type ExampleList,
  type HarnessOption,
  type HelperFeature,
  type HelperRequest,
  helperFeaturesForRequest,
  type InstanceList,
  type LiveInstanceRecord,
  type LoginStarted,
  type LoginUnavailable,
  type MachineDescription,
  type PathEntry,
  parseHelperRequest,
  type RunSummary,
  type TaskGoal,
  type UnreadableRecord,
  type UsageWindowReply,
} from '@vibesys/contracts/host-helper';
import {ConnectionError} from '../shell/helper-channel.js';
import type {Link} from '../shell/link.js';
import type {SpawnedProcess} from '../shell/process.js';
import type {Cause} from '../state/host-connection.js';
import {FakeLogStore, UnknownLogStream} from './fake-log-store.js';
import {type FakeProcess, fakeProcess} from './fake-process.js';

type Reply = Record<string, unknown>;

/** One explicitly deferred helper reply, released by a deterministic test action. */
export interface DeferredAnswer {
  /** Resolves after the helper handled the request and is holding its reply. */
  readonly received: Promise<void>;
  release(): void;
}

class PendingAnswer implements DeferredAnswer {
  readonly received: Promise<void>;
  readonly #received: () => void;
  #answer: (() => void) | null = null;
  #released = false;

  constructor() {
    let received = (): void => {};
    this.received = new Promise<void>(resolve => {
      received = resolve;
    });
    this.#received = received;
  }

  hold(answer: () => void): void {
    this.#answer = answer;
    this.#received();
    this.#answerIfReleased();
  }

  release(): void {
    this.#released = true;
    this.#answerIfReleased();
  }

  #answerIfReleased(): void {
    if (!this.#released || this.#answer === null) return;
    const answer = this.#answer;
    this.#answer = null;
    answer();
  }
}

/**
 * A run's server end: it gets the run channel's bytes and writes the server's. `end` ends the
 * relay with `outcome` (default `server_closed`), as the helper's `ChannelEnd` line reports it.
 */
export type RunServer = (stream: {
  write(line: string): void;
  end(outcome?: ChannelEndOutcome): void;
  onLine(listener: (line: string) => void): void;
}) => void;

/** An echo server: each line comes back as `{"echo": LINE}`. */
export const ECHO_SERVER: RunServer = stream =>
  stream.onLine(line => stream.write(JSON.stringify({echo: line})));

export interface FakeHost {
  readonly instances: LiveInstanceRecord[];
  readonly ended: LiveInstanceRecord[];
  readonly unreadable: UnreadableRecord[];
  readonly unverified: string[];
  /** Project directories and their tasks. */
  readonly projects: Map<string, readonly string[]>;
  /** What `project.task_goal` answers per `PROJECT TASK` (a space between); a task not named is `task_invalid`. */
  readonly goals: Map<string, TaskGoal>;
  /**
   * The pages `runs.summary` answers per `PROJECT RUN_ID` (a space between), each served for its
   * `offset`; a page not named here, or a run not named, is an unknown run.
   */
  readonly summaries: Map<string, readonly {readonly offset: number; readonly page: Reply}[]>;
  /** Directories and the directories in them. */
  readonly directories: Map<string, readonly string[]>;
  /** The runs each project records (`runs.list`), with the digests it can read. */
  readonly runHistory: Map<string, readonly RunSummary[]>;
  /** How each live run answers its run channel. */
  readonly runs: Map<string, RunServer>;
  /** How a read-only server (`run.reader`) answers its run channel. */
  readerServer: RunServer;
  /**
   * The read-only servers `run.reader` started, by the run each serves: a run with a live server
   * is read through it, and a run that already has a reader shares it.
   */
  readonly readers: Map<string, string>;
  /** The run of every read-only server `run.reader` started, in order. */
  readonly readerStarts: string[];
  /** Live runs this user may not connect to (`connect_denied`). */
  readonly denied: Set<string>;
  /** Retained bounded diagnostic tails, by instance id. */
  readonly logs: Map<string, {readonly text: string; readonly truncated: boolean}>;
  /** The vetted provider and model pairs (`harnesses.list`), each provider's default first. */
  readonly harnesses: HarnessOption[];
  /** What `machine.describe` answers; replace it to describe another machine. */
  machine: MachineDescription;
  /** What `checkout.inspect` answers per path; any other path holds no checkout. */
  readonly checkouts: Map<string, CheckoutInspectReply['inspection']>;
  /** What `run.preflight` answers for a known project; replace it to fail a check. */
  preflight: Check[];
  /** The role ids `run.preflight` reports the run's orchestration declares (`with_roles`). */
  roles: string[];
  /** The compute backend `run.preflight` reports the run resolved to (`with_backend`). */
  backend: Backend | null;
  /**
   * What `examples.list` answers (its `task_details` only when the request set `with_catalogue`),
   * or `null` for a helper with no catalog (`examples_unavailable`, as from an installed
   * distribution). `project.create_from_example` and `project.copy_out` add
   * the project they make to `projects` and `directories`.
   */
  examples: ExampleList | null;
  /** What `compute.list` answers; adds and removals change it, as they change the helper's file. */
  compute: ComputeSettings;
  /**
   * What `compute.test_cluster` finds, by the cluster's ssh host alias (a login-node cluster is
   * under `''`); a cluster not named here cannot be reached.
   */
  readonly clusterTests: Map<string, ClusterTestPassed | ClusterTestFailed>;
  /** What `agent_logins.list` answers. */
  logins: AgentLogin[];
  /** What `agent_logins.start` answers per provider; a provider not named has no login command. */
  readonly loginStarts: Map<string, LoginStarted | LoginUnavailable>;
  /**
   * What `usage.window` answers per project: the figures it holds, whatever the window. The reply
   * echoes the window asked for and names as `missing_runs` the requested runs the project's
   * `runHistory` does not list; a project not named here has spent nothing.
   */
  readonly usage: Map<string, UsageFigures>;
}

/** The figures of a `usage.window` reply that do not depend on which window was asked. */
type UsageFigures = Omit<
  UsageWindowReply,
  'type' | 'request_id' | 'since' | 'until' | 'missing_runs'
>;

/** A project that spent nothing: the figures `usage.window` answers for a project with no entry. */
const NO_USAGE: UsageFigures = {
  spent_usd: 0,
  spend_per_hour_usd: 0,
  tokens: {input: null, cache_read: null, cache_write: null, output: null, reasoning: null},
  turns: 0,
  unpriced_turns: 0,
  untimed_turns: 0,
  new_bests: 0,
  cost_per_new_best_usd: null,
  by_run: [],
  by_agent: [],
  agents_omitted: 0,
};

/** `listing` as a helper answers a client that does not set `with_catalogue`. */
function withoutTaskDetails(listing: ExampleList): ExampleList {
  return {
    ...listing,
    examples: listing.examples.map(({task_details: _details, ...example}) => example),
  };
}

/** `check` as a helper answers a client that sets (or not) `with_backend` and `with_causes`. */
function answeredTo(check: Check, request: Extract<HelperRequest, {type: 'run.preflight'}>): Check {
  const {cause} = check;
  if (cause === undefined || cause === null || cause.kind === 'project_dirty') return check;
  const asked = cause.kind === 'backend_unavailable' ? request.with_backend : request.with_causes;
  if (asked === true) return check;
  const {cause: _withheld, ...plain} = check;
  return plain;
}

/** The roles a new `FakeHelper` reports: those the multi orchestration declares. */
const FAKE_ROLES: readonly string[] = [
  'multi-orchestrator',
  'multi-profiler',
  'multi-implementer',
  'multi-judge',
];

/** The checks a new `FakeHelper` answers: every one passes. */
const PASSING_CHECKS: readonly Check[] = [
  {id: 'checkout_found', state: 'ok', detail: 'VibeSys 0.1.1 at /src/vibesys'},
  {id: 'project_own_repository', state: 'ok', detail: 'the project is its own Git repository'},
  {id: 'project_clean', state: 'ok', detail: 'the project has no uncommitted changes'},
  {id: 'settings_valid', state: 'ok', detail: "the run's configuration is valid"},
  {id: 'docker_reachable', state: 'ok', detail: 'Docker 27.3.1'},
  {id: 'agent_image', state: 'ok', detail: 'an agent image is cached here'},
  {id: 'evaluator_available', state: 'ok', detail: 'benchmark command'},
];

/** The machine a new `FakeHelper` describes: a CPU-only host with Docker and one harness CLI. */
const FAKE_MACHINE: MachineDescription = {
  checkout: {root: '/src/vibesys', version: '0.1.1', commit: 'abc123', dirty: false},
  hardware: {cpu_cores: 8, gpus: [], accelerator: 'cpu'},
  docker: {reachable: true, version: '27.3.1'},
  harness_clis: ['codex'],
  runs: {live: 0, ended_today: 0},
};

/** The pairs a new `FakeHelper` offers: two providers, each default first. */
export const FAKE_HARNESSES: readonly HarnessOption[] = [
  {provider: 'codex', model: 'gpt-a', default: true},
  {provider: 'codex', model: 'gpt-b', default: false},
  {provider: 'claude', model: 'claude-a', default: true},
];

/**
 * The pairs of a helper whose agent library names each model's maker and generation: two makers,
 * the newer generation of one with two tiers, an older generation still vetted, and a model
 * agentshim places in no generation. A strict client sees the maker and generation only when it
 * asks (`with_generations`), so use `FakeHelper.host.harnesses` to seed it.
 */
export const FAKE_MODEL_CATALOGUE: readonly HarnessOption[] = [
  {
    provider: 'codex',
    model: 'gpt-6-sol',
    default: true,
    display: {
      provider_name: 'Codex',
      model_name: 'GPT-6 Sol',
      family: 'GPT',
      tier: 3,
      tier_name: 'Sol',
      maker: 'openai',
      generation: {id: 'gpt-6', name: 'GPT-6', order: 1},
    },
  },
  {
    provider: 'codex',
    model: 'gpt-6-luna',
    default: false,
    display: {
      provider_name: 'Codex',
      model_name: 'GPT-6 Luna',
      family: 'GPT',
      tier: 1,
      tier_name: 'Luna',
      maker: 'openai',
      generation: {id: 'gpt-6', name: 'GPT-6', order: 1},
    },
  },
  {
    provider: 'codex',
    model: 'gpt-5.6-sol',
    default: false,
    display: {
      provider_name: 'Codex',
      model_name: 'GPT-5.6 Sol',
      family: 'GPT',
      tier: 3,
      tier_name: 'Sol',
      maker: 'openai',
      generation: {id: 'gpt-5.6', name: 'GPT-5.6', order: 2},
    },
  },
  {
    provider: 'codex',
    model: 'gpt-5.5',
    default: false,
    display: {provider_name: 'Codex', model_name: 'GPT-5.5', family: 'GPT', maker: 'openai'},
  },
  {
    provider: 'claude',
    model: 'claude-opus-5-5',
    default: true,
    display: {
      provider_name: 'Claude Code',
      model_name: 'Opus 5.5',
      family: 'Claude',
      tier: 3,
      tier_name: 'Opus',
      maker: 'anthropic',
      generation: {id: 'claude-5.5', name: 'Claude 5.5', order: 1},
    },
  },
  {
    provider: 'claude',
    model: 'claude-sonnet-5',
    default: false,
    display: {
      provider_name: 'Claude Code',
      model_name: 'Sonnet 5',
      family: 'Claude',
      tier: 2,
      tier_name: 'Sonnet',
      maker: 'anthropic',
      generation: {id: 'claude-5', name: 'Claude 5', order: 2},
    },
  },
];

/** The same pairs as a client that did not ask for generations reads them. */
function withoutGenerations(option: HarnessOption): HarnessOption {
  if (option.display == null) return option;
  const {maker: _maker, generation: _generation, ...display} = option.display;
  return {...option, display};
}

/**
 * The run options `server.host_api` names (`UNVETTED_MODEL_FLAG`, `CLI_PROVIDER_FLAG`,
 * `MODEL_FLAG`): the wire contract carries no constants, so this Fake repeats them.
 */
const UNVETTED_MODEL_FLAG = '--allow-unvetted-model';
const HARNESS_FLAGS = ['--cli-provider', '--model'] as const;
const ROLE_AGENT_FLAGS = ['--role-provider', '--role-model'] as const;

/** Whether argparse reads `token` (`--name` or `--name=value`) as `flag`, as the helper decides. */
function abbreviates(token: string, flag: string): boolean {
  const name = token.split('=', 1)[0] ?? '';
  return name.startsWith('--') && name.length > 2 && flag.startsWith(name);
}

/** Why the helper refuses `start`'s arguments, or null when it accepts them. */
function refusedArgument(
  start: Extract<HelperRequest, {type: 'run.start' | 'run.preflight'}>,
): string | null {
  const refused = [
    UNVETTED_MODEL_FLAG,
    ...(start.harness == null ? [] : HARNESS_FLAGS),
    ...(start.agents == null || Object.keys(start.agents).length === 0 ? [] : ROLE_AGENT_FLAGS),
  ];
  for (const token of start.args ?? []) {
    const flag = refused.find(candidate => abbreviates(token, candidate));
    if (flag !== undefined) return `${token} selects ${flag}`;
  }
  return null;
}

export function record(id: string, runId: string, project = '/p'): LiveInstanceRecord {
  return {
    version: 5,
    id,
    status: 'serving',
    phase: {status: 'running', stage: null, reason: null},
    socket_path: `/tmp/${id}.sock`,
    log_path: `/tmp/${id}.log`,
    project_root: project,
    run_id: runId,
    pid: 4242,
    started_at: 1,
    ended_at: null,
    hostname: 'fake',
    protocol_version: 1,
    vibesys_version: '0.1.1',
    vibesys_root: '/src/vibesys',
  };
}

export class FakeHelper {
  readonly host: FakeHost = {
    instances: [],
    ended: [],
    unreadable: [],
    unverified: [],
    projects: new Map(),
    goals: new Map(),
    summaries: new Map(),
    directories: new Map(),
    runHistory: new Map(),
    runs: new Map(),
    readerServer: ECHO_SERVER,
    readers: new Map(),
    readerStarts: [],
    denied: new Set(),
    logs: new Map(),
    harnesses: [...FAKE_HARNESSES],
    machine: FAKE_MACHINE,
    checkouts: new Map(),
    preflight: [...PASSING_CHECKS],
    roles: [...FAKE_ROLES],
    backend: 'cpu',
    examples: null,
    compute: {
      config_path: '/home/me/.config/vibesys/compute.toml',
      clusters: [],
      modal: {token_present: false},
    },
    clusterTests: new Map(),
    logins: [],
    loginStarts: new Map(),
    usage: new Map(),
  };
  /** Exit every new channel at once with this status and stdout, as a failing bootstrap does. */
  bootstrapExit: {readonly code: number; readonly stdout: string} | null = null;
  /** Lines every new channel prints before its helper starts, as a bootstrap reporting `uv`. */
  preamble: readonly string[] = [];
  /** Channels answer nothing, `hello` included, until `answerHeld`. */
  silent = false;
  /** Request types answered with `deadline_exceeded`. */
  readonly expired = new Set<string>();
  /** Request types answered with a reply the contract rejects (an unknown field). */
  readonly garbled = new Set<string>();
  /** Every new channel's helper exits with this status and stdout right after answering `hello`. */
  exitAfterHello: {readonly code: number; readonly stdout: string} | null = null;
  /** Optional first run-protocol line written with the successful `run.connect` reply. */
  runGreeting: string | null = null;
  /** How that combined reply/greeting payload reaches stdout; tests may split it at any byte. */
  runConnectChunks: (payload: Buffer) => readonly Buffer[] = payload => [payload];
  /** Every channel ever started, in order. */
  readonly channels: FakeProcess[] = [];
  /** How many helpers were started: one per channel. */
  spawned = 0;
  /** Stop requests received, in order. */
  readonly stops: {readonly instanceId: string; readonly force: boolean}[] = [];
  /** Every valid helper request received, including each channel's hello. */
  readonly requests: HelperRequest[] = [];
  /** Features this helper version recognizes. */
  readonly features = new Set<HelperFeature>([
    'instances_watch',
    'paths_complete',
    'instances_inspect',
    'harnesses',
    'logs',
    'logs_cancel',
    'run_history',
    'run_span_events',
    'run_tail',
    'run_summary',
    'run_trace',
    'run_ending',
    'run_history_pages',
    'machine_describe',
    'checkout_inspect',
    'preflight',
    'run_options',
    'examples',
    'run_roles',
    'run_backend',
    'run_causes',
    'project_clean',
    'run_digest',
    'run_dashboard',
    'example_catalogue',
    'harness_generations',
    'run_role_agents',
    'measurement_target',
    'machine_compute',
    'agent_logins',
    'project_goal',
    'example_destination',
    'usage_window',
    'chat_ended_runs',
    'chat_ended_run_questions',
  ]);
  /** The run log streams `logs.list` and `logs.read` serve; replace it to seed a store. */
  logStore = new FakeLogStore();
  readonly #pendingAnswers = new Map<HelperRequest['type'], PendingAnswer[]>();
  /** How many of them have exited. */
  exited = 0;
  /** Every checkout a helper was started from, in order. */
  readonly checkouts: string[] = [];
  readonly #watches = new Set<() => void>();
  readonly #held: (() => void)[] = [];
  #nextInstance = 1;

  /** Start one helper from `checkout`: a new channel. */
  spawn(checkout = '/src/vibesys'): SpawnedProcess {
    this.spawned += 1;
    this.checkouts.push(checkout);
    const channel = fakeProcess();
    this.channels.push(channel);
    void channel.process.exit.then(() => (this.exited += 1));
    if (this.bootstrapExit !== null) {
      channel.end(this.bootstrapExit.code, `${this.bootstrapExit.stdout}\n`);
      return channel.process;
    }
    for (const line of this.preamble) channel.stdout.write(`${line}\n`);
    new HelperSession(this, channel).start();
    return channel.process;
  }

  /** Handle the next request of `type`, but hold its correlated reply until this gate is released. */
  deferNext(type: HelperRequest['type']): DeferredAnswer {
    const pending = new PendingAnswer();
    const queue = this.#pendingAnswers.get(type) ?? [];
    queue.push(pending);
    this.#pendingAnswers.set(type, queue);
    return pending;
  }

  /** Take the reply gate registered for the next request of `type`, if any. */
  takeDeferred(type: HelperRequest['type']): PendingAnswer | null {
    const queue = this.#pendingAnswers.get(type);
    const pending = queue?.shift();
    if (pending === undefined) return null;
    if (queue?.length === 0) this.#pendingAnswers.delete(type);
    return pending;
  }

  /** Helpers started and not yet exited. */
  get running(): number {
    return this.spawned - this.exited;
  }

  /** Registry watches being served. */
  get watching(): number {
    return this.#watches.size;
  }

  /** Keep a silent helper's answer until `answerHeld`. */
  hold(answer: () => void): void {
    this.#held.push(answer);
  }

  /** Stop being silent: answer, late, everything held (a channel that ended writes nothing). */
  answerHeld(): void {
    this.silent = false;
    for (const answer of this.#held.splice(0)) answer();
  }

  /** Publish a live record; watches push it. */
  publish(live: LiveInstanceRecord): void {
    this.host.unreadable.splice(
      0,
      this.host.unreadable.length,
      ...this.host.unreadable.filter(entry => entry.id !== live.id),
    );
    this.host.unverified.splice(
      0,
      this.host.unverified.length,
      ...this.host.unverified.filter(id => id !== live.id),
    );
    this.host.instances.push(live);
    this.changed();
  }

  /** Replace a live record with an observation whose lock could not be read. */
  unverify(id: string): void {
    this.host.instances.splice(
      0,
      this.host.instances.length,
      ...this.host.instances.filter(entry => entry.id !== id),
    );
    this.host.unreadable.splice(
      0,
      this.host.unreadable.length,
      ...this.host.unreadable.filter(entry => entry.id !== id),
    );
    if (!this.host.unverified.includes(id)) this.host.unverified.push(id);
    this.changed();
  }

  /** Replace a live record with one this helper cannot interpret. */
  makeUnreadable(id: string, version = 5): void {
    this.host.instances.splice(
      0,
      this.host.instances.length,
      ...this.host.instances.filter(entry => entry.id !== id),
    );
    this.host.unverified.splice(
      0,
      this.host.unverified.length,
      ...this.host.unverified.filter(entry => entry !== id),
    );
    if (!this.host.unreadable.some(entry => entry.id === id)) {
      this.host.unreadable.push({id, version});
    }
    this.changed();
  }

  /** End the live server `id`, keeping its record as a tombstone. */
  end(id: string, status: 'stopped' | 'failed' | 'completed' = 'stopped'): void {
    const index = this.host.instances.findIndex(entry => entry.id === id);
    const [ended] = this.host.instances.splice(index, 1);
    if (ended !== undefined) {
      this.host.ended.push({...ended, phase: {status, stage: null, reason: null}, ended_at: 2});
    }
    this.changed();
  }

  /** Record and perform one host stop; returns whether the run was live. */
  stop(instanceId: string, force: boolean): boolean {
    this.stops.push({instanceId, force});
    const live = this.host.instances.some(entry => entry.id === instanceId);
    if (live) this.end(instanceId);
    return live;
  }

  /** Every channel ends at once, as when the link to the host breaks. */
  drop(): void {
    for (const channel of this.channels) channel.end(null);
  }

  listing(): InstanceList {
    return {
      version: 5,
      instances: [...this.host.instances],
      ended: [...this.host.ended],
      unreadable: [...this.host.unreadable],
      unverified: [...this.host.unverified],
    };
  }

  changed(): void {
    for (const push of this.#watches) push();
  }

  watch(push: () => void): () => void {
    this.#watches.add(push);
    return () => this.#watches.delete(push);
  }

  newInstanceId(): string {
    return (this.#nextInstance++).toString(16).padStart(12, '0');
  }

  /** A read-only server ended by itself, as it does once no client holds it: the next `run.reader` starts another. */
  endReader(runId: string): void {
    this.host.readers.delete(runId);
  }
}

/** One channel's helper: the handshake, requests, watches, and the hand-over to a run. */
class HelperSession {
  readonly #helper: FakeHelper;
  readonly #channel: FakeProcess;
  readonly #watches = new Map<string, () => void>();
  /** Correlated request replies held after their request's side effects have happened. */
  readonly #deferredReplies = new Map<string, PendingAnswer>();
  #buffer = '';
  #features: string[] | null = null;
  #relay: ((line: string) => void) | null = null;

  constructor(helper: FakeHelper, channel: FakeProcess) {
    this.#helper = helper;
    this.#channel = channel;
  }

  start(): void {
    const input = this.#channel.input;
    input.setEncoding('utf8');
    input.on('data', (chunk: string) => {
      this.#buffer += chunk;
      let newline = this.#buffer.indexOf('\n');
      while (newline >= 0) {
        const line = this.#buffer.slice(0, newline);
        this.#buffer = this.#buffer.slice(newline + 1);
        if (this.#relay !== null) this.#relay(line);
        else this.#line(line);
        newline = this.#buffer.indexOf('\n');
      }
    });
    input.on('end', () => this.#close(0));
    input.on('close', () => this.#close(0));
  }

  #close(code: number | null, stderr = ''): void {
    for (const stop of this.#watches.values()) stop();
    this.#watches.clear();
    this.#channel.end(code, '', stderr);
  }

  #send(reply: Reply, trailing = ''): void {
    const send = (): void => {
      if (this.#channel.stdout.writableEnded) return;
      const payload = Buffer.from(`${JSON.stringify(reply)}\n${trailing}`);
      const chunks = trailing === '' ? [payload] : this.#helper.runConnectChunks(payload);
      for (const chunk of chunks) this.#channel.stdout.write(chunk);
    };
    const requestId = reply['request_id'];
    const pending =
      typeof requestId === 'string' ? this.#deferredReplies.get(requestId) : undefined;
    if (pending === undefined) {
      send();
      return;
    }
    this.#deferredReplies.delete(requestId as string);
    pending.hold(send);
  }

  #error(requestId: string | null, code: string, message: string): Reply {
    return {type: 'error', request_id: requestId, code, message, path: null};
  }

  #line(line: string): void {
    let request: HelperRequest;
    try {
      request = parseHelperRequest(line);
    } catch (error) {
      const id = requestIdOf(line);
      this.#send(this.#error(id, 'invalid_request', (error as Error).message));
      return;
    }
    this.#helper.requests.push(request);
    const helper = this.#helper;
    if (helper.silent) {
      helper.hold(() => this.#respond(request));
      return;
    }
    this.#respond(request);
  }

  #respond(request: HelperRequest): void {
    const helper = this.#helper;
    const pending = helper.takeDeferred(request.type);
    if (pending !== null) this.#deferredReplies.set(request.request_id, pending);
    if (this.#features === null && request.type !== 'hello') {
      this.#send(this.#error(request.request_id, 'handshake_required', 'hello first'));
      return;
    }
    const required = helperFeaturesForRequest(request).find(
      feature => this.#features?.includes(feature) !== true,
    );
    if (request.type !== 'hello' && required !== undefined) {
      this.#send(this.#error(request.request_id, 'feature_not_negotiated', required));
      return;
    }
    if (helper.expired.has(request.type)) {
      this.#send(this.#error(request.request_id, 'deadline_exceeded', 'past its deadline'));
      return;
    }
    if (helper.garbled.has(request.type)) {
      this.#send({type: request.type, request_id: request.request_id, unexpected: true});
      return;
    }
    this.#answer(request);
    const crash = helper.exitAfterHello;
    if (request.type === 'hello' && crash !== null) {
      this.#channel.end(crash.code, `${crash.stdout}\n`);
    }
  }

  #answer(request: HelperRequest): void {
    const id = request.request_id;
    switch (request.type) {
      case 'hello':
        this.#hello(request);
        return;
      case 'ping':
        this.#send({type: 'ping', request_id: id});
        return;
      case 'instances.list':
        this.#send({type: 'instances.list', request_id: id, listing: this.#helper.listing()});
        return;
      case 'instances.inspect':
        this.#inspect(request);
        return;
      case 'instances.watch':
        this.#watch(request);
        return;
      case 'instances.unwatch':
        this.#unwatch(request);
        return;
      case 'tasks.list':
      case 'runs.list':
      case 'project.task_goal':
        this.#project(request);
        return;
      case 'run.start':
        this.#start(request);
        return;
      case 'runs.read_events':
      case 'runs.events_for_span':
      case 'runs.read_tail':
      case 'runs.summary':
      case 'usage.window':
        this.#readRunHistory(request);
        return;
      case 'machine.describe':
        this.#send({type: 'machine.describe', request_id: id, machine: this.#helper.host.machine});
        return;
      case 'run.preflight':
        this.#preflight(request);
        return;
      case 'examples.list':
      case 'examples.destination':
        this.#examples(request);
        return;
      case 'project.create_from_example':
      case 'project.copy_out':
        this.#createProject(request);
        return;
      case 'checkout.inspect':
        this.#inspectCheckout(request);
        return;
      case 'harnesses.list':
        this.#harnesses(request);
        return;
      case 'compute.list':
      case 'compute.test_cluster':
      case 'compute.add_cluster':
      case 'compute.remove_cluster':
      case 'agent_logins.list':
      case 'agent_logins.start':
        this.#machineSettings(request);
        return;
      case 'run.stop':
      case 'run.reader':
      case 'run.connect':
        this.#runRequest(request);
        return;
      case 'paths.complete':
        this.#complete(id, request.prefix);
        return;
      case 'logs.list':
      case 'logs.read':
        this.#logs(request);
        return;
      case 'logs.cancel':
        // This helper answers every read at once, so the named read has always answered already
        // (or never existed): the cancel's reply follows it, as the real helper's does.
        this.#send({type: 'logs.cancel', request_id: id, read_id: request.read_id});
        return;
    }
  }

  /** The inspection of a checkout the host holds, or `not_found` when it holds none there. */
  #inspectCheckout(request: Extract<HelperRequest, {type: 'checkout.inspect'}>): void {
    this.#send({
      type: 'checkout.inspect',
      request_id: request.request_id,
      inspection: this.#helper.host.checkouts.get(request.path) ?? {state: 'not_found'},
    });
  }

  /** The compute settings and agent login requests, which read or change what the host holds. */
  #machineSettings(
    request: Extract<
      HelperRequest,
      {
        type:
          | 'compute.list'
          | 'compute.test_cluster'
          | 'compute.add_cluster'
          | 'compute.remove_cluster'
          | 'agent_logins.list'
          | 'agent_logins.start';
      }
    >,
  ): void {
    const id = request.request_id;
    const host = this.#helper.host;
    switch (request.type) {
      case 'compute.list':
        this.#send({type: 'compute.list', request_id: id, settings: host.compute});
        return;
      case 'compute.test_cluster':
        this.#send({
          type: 'compute.test_cluster',
          request_id: id,
          result: this.#testCluster(request.cluster),
        });
        return;
      case 'compute.add_cluster':
        this.#addCluster(request);
        return;
      case 'compute.remove_cluster':
        this.#removeCluster(request);
        return;
      case 'agent_logins.list':
        this.#send({type: 'agent_logins.list', request_id: id, logins: host.logins});
        return;
      case 'agent_logins.start':
        this.#send({
          type: 'agent_logins.start',
          request_id: id,
          result: host.loginStarts.get(request.provider) ?? {
            outcome: 'unavailable',
            reason: 'no_login_command',
          },
        });
        return;
    }
  }

  /** The figures the host holds for the (known) project, in the window asked for. */
  #usage(request: Extract<HelperRequest, {type: 'usage.window'}>): void {
    const id = request.request_id;
    const host = this.#helper.host;
    const known = new Set((host.runHistory.get(request.project) ?? []).map(run => run.run_id));
    this.#send({
      ...(host.usage.get(request.project) ?? NO_USAGE),
      type: 'usage.window',
      request_id: id,
      since: request.since,
      until: request.until,
      missing_runs: [...new Set(request.run_ids)].filter(run => !known.has(run)),
    });
  }

  #harnesses(request: Extract<HelperRequest, {type: 'harnesses.list'}>): void {
    const {harnesses} = this.#helper.host;
    this.#send({
      type: 'harnesses.list',
      request_id: request.request_id,
      harnesses:
        request.with_generations === true ? [...harnesses] : harnesses.map(withoutGenerations),
    });
  }

  #testCluster(
    cluster: Extract<HelperRequest, {type: 'compute.test_cluster'}>['cluster'],
  ): ClusterTestPassed | ClusterTestFailed {
    const alias = cluster.reach.kind === 'ssh' ? cluster.reach.host : '';
    return (
      this.#helper.host.clusterTests.get(alias) ?? {
        outcome: 'failed',
        stage: 'reach',
        detail: `cannot reach ${alias || 'the scheduler'}`,
      }
    );
  }

  #addCluster(request: Extract<HelperRequest, {type: 'compute.add_cluster'}>): void {
    const id = request.request_id;
    const host = this.#helper.host;
    if (host.compute.clusters.some(listed => listed.cluster.name === request.cluster.name)) {
      this.#send(
        this.#error(
          id,
          'cluster_exists',
          `a cluster named '${request.cluster.name}' is already set up`,
        ),
      );
      return;
    }
    const result = this.#testCluster(request.cluster);
    if (result.outcome === 'failed') {
      this.#send(this.#error(id, 'cluster_test_failed', `${result.stage}: ${result.detail}`));
      return;
    }
    host.compute = {
      ...host.compute,
      clusters: [...host.compute.clusters, {cluster: request.cluster, source: 'vibesys'}],
    };
    this.#send({type: 'compute.add_cluster', request_id: id, settings: host.compute});
  }

  #removeCluster(request: Extract<HelperRequest, {type: 'compute.remove_cluster'}>): void {
    const id = request.request_id;
    const host = this.#helper.host;
    const listed = host.compute.clusters.find(each => each.cluster.name === request.name);
    if (listed === undefined) {
      this.#send(
        this.#error(id, 'unknown_cluster', `no cluster named '${request.name}' is set up`),
      );
      return;
    }
    if (listed.source === 'slurm_toml') {
      this.#send(
        this.#error(
          id,
          'cluster_read_only',
          `cluster '${request.name}' comes from the operator file`,
        ),
      );
      return;
    }
    host.compute = {
      ...host.compute,
      clusters: host.compute.clusters.filter(each => each !== listed),
    };
    this.#send({type: 'compute.remove_cluster', request_id: id, settings: host.compute});
  }

  #examples(
    request: Extract<HelperRequest, {type: 'examples.list' | 'examples.destination'}>,
  ): void {
    if (request.type === 'examples.destination') {
      this.#exampleDestination(request);
      return;
    }
    const listing = this.#helper.host.examples;
    if (listing === null) {
      this.#send({
        ...this.#error(request.request_id, 'examples_unavailable', 'no catalog'),
        path: '',
      });
      return;
    }
    this.#send({
      type: 'examples.list',
      request_id: request.request_id,
      listing: request.with_catalogue === true ? listing : withoutTaskDetails(listing),
    });
  }

  /**
   * What `project.create_from_example` would find at the destination: nothing, an empty
   * directory, a project with the example's tasks, or anything else.
   */
  #exampleDestination(request: Extract<HelperRequest, {type: 'examples.destination'}>): void {
    const id = request.request_id;
    const {host} = this.#helper;
    const example = host.examples?.examples.find(entry => entry.name === request.example);
    if (host.examples === null || example === undefined) {
      const [code, path] =
        host.examples === null
          ? (['examples_unavailable', ''] as const)
          : (['unknown_example', host.examples.checkout] as const);
      this.#send({...this.#error(id, code, 'no such example'), path});
      return;
    }
    const {destination} = request;
    const tasks = host.projects.get(destination);
    const entries = host.directories.get(destination);
    const same =
      tasks !== undefined &&
      example.tasks.length > 0 &&
      [...tasks].sort().join('\n') === [...example.tasks].sort().join('\n');
    const state =
      tasks !== undefined
        ? same
          ? 'example_project'
          : 'occupied'
        : entries === undefined
          ? 'absent'
          : entries.length === 0
            ? 'empty'
            : 'occupied';
    this.#send({
      type: 'examples.destination',
      request_id: id,
      state,
      project: state === 'example_project' ? destination : null,
    });
  }

  #logs(request: Extract<HelperRequest, {type: 'logs.list' | 'logs.read'}>): void {
    const helper = this.#helper;
    const id = request.request_id;
    const known =
      helper.logStore.has(request.instance_id) ||
      [...helper.host.instances, ...helper.host.ended].some(
        entry => entry.id === request.instance_id,
      );
    if (!known) {
      this.#send(this.#error(id, 'unknown_instance', `no logs for ${request.instance_id}`));
      return;
    }
    if (request.type === 'logs.list') {
      const streams = helper.logStore.streams(request.instance_id);
      this.#send({type: 'logs.list', request_id: id, streams});
      return;
    }
    try {
      const chunk = helper.logStore.read(
        request.instance_id,
        request.stream_id,
        request.start,
        request.max_bytes,
      );
      this.#send({type: 'logs.read', request_id: id, chunk});
    } catch (error) {
      if (!(error instanceof UnknownLogStream)) throw error;
      this.#send(this.#error(id, 'unknown_log_stream', `no log stream ${request.stream_id}`));
    }
  }

  #hello(request: Extract<HelperRequest, {type: 'hello'}>): void {
    const [low, high] = request.client_versions as [number, number];
    if (this.#features !== null || low > 1 || high < 1) {
      this.#send({
        ...this.#error(request.request_id, 'incompatible', 'versions'),
        supported_versions: [1, 1],
        vibesys_version: '0.1.1',
      });
      return;
    }
    const supported: ReadonlySet<string> = this.#helper.features;
    this.#features = (request.features ?? []).filter(feature => supported.has(feature));
    this.#send({
      type: 'hello',
      request_id: request.request_id,
      protocol_version: 1,
      features: this.#features,
      vibesys_version: '0.1.1',
      checkout: {root: '/src/vibesys', commit: null, dirty: null},
    });
  }

  #inspect(request: Extract<HelperRequest, {type: 'instances.inspect'}>): void {
    const helper = this.#helper;
    const record = [...helper.host.instances, ...helper.host.ended].find(
      entry => entry.id === request.instance_id,
    );
    const retained = helper.host.logs.get(request.instance_id);
    const log = request.include_log
      ? retained === undefined
        ? {state: 'missing' as const}
        : {state: 'available' as const, ...retained}
      : null;
    this.#send({
      type: 'instances.inspect',
      request_id: request.request_id,
      inspection: {
        version: 1,
        id: request.instance_id,
        state:
          record === undefined
            ? 'ended'
            : helper.host.instances.includes(record)
              ? 'live'
              : 'ended',
        record: record ?? null,
        metadata: null,
        control_socket_path: record?.socket_path ?? `/tmp/${request.instance_id}.sock`,
        server_log_path: record?.log_path ?? `/tmp/${request.instance_id}.log`,
        log,
      },
    });
  }

  #watch(request: Extract<HelperRequest, {type: 'instances.watch'}>): void {
    const id = request.request_id;
    this.#send({type: 'instances.watch', request_id: id, watch_id: id});
    const push = (): void =>
      this.#send({type: 'instances.changed', watch_id: id, listing: this.#helper.listing()});
    this.#watches.set(id, this.#helper.watch(push));
    push();
  }

  #unwatch(request: Extract<HelperRequest, {type: 'instances.unwatch'}>): void {
    const stop = this.#watches.get(request.watch_id);
    if (stop === undefined) {
      this.#send(this.#error(request.request_id, 'unknown_watch', 'no such watch'));
      return;
    }
    stop();
    this.#watches.delete(request.watch_id);
    this.#send({
      type: 'instances.unwatch',
      request_id: request.request_id,
      watch_id: request.watch_id,
    });
  }

  #stop(request: Extract<HelperRequest, {type: 'run.stop'}>): void {
    const live = this.#helper.stop(request.instance_id, request.force ?? false);
    this.#send({
      type: 'run.stop',
      request_id: request.request_id,
      result: {
        version: 1,
        id: request.instance_id,
        outcome: live ? 'stopped' : 'not_running',
        route: null,
      },
    });
  }

  /** `run.start`: its arguments and harness are checked before its project, as the helper does. */
  #start(request: Extract<HelperRequest, {type: 'run.start'}>): void {
    const id = request.request_id;
    const refused = refusedArgument(request);
    if (refused !== null) {
      this.#send(this.#error(id, 'invalid_request', refused));
      return;
    }
    const refusal = this.#refusedHarness(request);
    if (refusal !== null) {
      this.#send(refusal);
      return;
    }
    if (!this.#helper.host.projects.has(request.project)) {
      this.#project(request);
      return;
    }
    const undeclared = this.#undeclaredAgent(request);
    if (undeclared !== null) {
      this.#send(this.#error(id, 'invalid_request', undeclared));
      return;
    }
    this.#project(request);
  }

  /**
   * Why the helper refuses the request's harness, its roles' agents or their models, or null:
   * a role's typed model and its agent both choose its model, and every pair must be offered.
   */
  #refusedHarness(
    request: Extract<HelperRequest, {type: 'run.start' | 'run.preflight'}>,
  ): Reply | null {
    const id = request.request_id;
    const offered = this.#helper.host.harnesses;
    const agents = Object.entries(request.agents ?? {});
    const modelled = request.options?.roles?.find(
      role => role.model != null && request.agents?.[role.role] !== undefined,
    );
    if (modelled !== undefined) {
      const message = `agents.${modelled.role} sets the model of role '${modelled.role}', which options.roles also sets; set it in one place`;
      return this.#error(id, 'invalid_request', message);
    }
    const choices = [
      ...(request.harness == null ? [] : [['harness', request.harness] as const]),
      ...agents.map(([role, choice]) => [`agents.${role}`, choice] as const),
    ];
    for (const [key, choice] of choices) {
      const vetted = offered.some(
        option => option.provider === choice.provider && option.model === choice.model,
      );
      if (vetted) continue;
      const allowed = offered
        .filter(option => option.provider === choice.provider)
        .map(option => option.model);
      const models = allowed.length === 0 ? 'none' : allowed.join(', ');
      return this.#error(
        id,
        'unvetted_harness',
        `${key}: model '${choice.model}' is not vetted for provider '${choice.provider}'; vetted models: ${models}`,
      );
    }
    return null;
  }

  /** The first `agents` key naming a role the run's orchestration does not declare, worded. */
  #undeclaredAgent(
    request: Extract<HelperRequest, {type: 'run.start' | 'run.preflight'}>,
  ): string | null {
    const declared = this.#helper.host.roles;
    const unknown = Object.keys(request.agents ?? {}).filter(role => !declared.includes(role));
    if (unknown.length === 0) return null;
    return `${unknown.map(role => `agents.${role}`).join(', ')}: the run's orchestration declares no such role`;
  }

  /**
   * Make a project as the helper would: an example the catalog lists, or a folder that exists,
   * into a destination nothing occupies yet. The new project defines the example's tasks.
   */
  #createProject(
    request: Extract<HelperRequest, {type: 'project.create_from_example' | 'project.copy_out'}>,
  ): void {
    const id = request.request_id;
    const host = this.#helper.host;
    const catalog = host.examples;
    if (request.type === 'project.create_from_example' && catalog === null) {
      this.#send({...this.#error(id, 'examples_unavailable', 'no catalog'), path: ''});
      return;
    }
    const example = catalog?.examples.find(
      entry => request.type === 'project.create_from_example' && entry.name === request.example,
    );
    const known =
      request.type === 'project.copy_out'
        ? host.directories.has(request.source)
        : example !== undefined;
    if (!known) {
      const [code, path] =
        request.type === 'project.copy_out'
          ? (['path_missing', request.source] as const)
          : (['unknown_example', catalog?.checkout ?? ''] as const);
      this.#send({...this.#error(id, code, 'no such source'), path});
      return;
    }
    // An empty directory is made into the project; a project, or a directory with entries, is not.
    const occupied = (host.directories.get(request.destination)?.length ?? 0) > 0;
    if (occupied || host.projects.has(request.destination)) {
      this.#send({
        ...this.#error(id, 'destination_exists', `${request.destination} exists`),
        path: request.destination,
      });
      return;
    }
    const tasks =
      request.type === 'project.copy_out'
        ? [...(host.projects.get(request.source) ?? [])]
        : [...(example?.tasks ?? [])];
    host.projects.set(request.destination, tasks);
    host.directories.set(request.destination, []);
    this.#send({
      type: request.type,
      request_id: id,
      created: {project: request.destination, tasks},
    });
  }

  #preflight(request: Extract<HelperRequest, {type: 'run.preflight'}>): void {
    const id = request.request_id;
    const argument = refusedArgument(request);
    const refusal = argument === null ? this.#refusedHarness(request) : null;
    if (argument !== null || refusal !== null) {
      this.#send(refusal ?? this.#error(id, 'invalid_request', argument ?? ''));
      return;
    }
    if (!this.#helper.host.projects.has(request.project)) {
      this.#send({...this.#error(id, 'project_missing', 'no such project'), path: request.project});
      return;
    }
    const undeclared = this.#undeclaredAgent(request);
    if (undeclared !== null) {
      this.#send(this.#error(id, 'invalid_request', undeclared));
      return;
    }
    const {host} = this.#helper;
    this.#send({
      type: 'run.preflight',
      request_id: id,
      // A helper answers the project_clean check only to a client that asked for it, and a typed
      // cause only to one that asked for its kind (the backend's, or any other failed setting's).
      checks: host.preflight
        .filter(check => request.with_project_clean === true || check.id !== 'project_clean')
        .map(check => answeredTo(check, request)),
      ...(request.with_roles === true ? {roles: [...host.roles]} : {}),
      ...(request.with_backend === true && host.backend !== null ? {backend: host.backend} : {}),
    });
  }

  /** The fake host records no runs, so every run of a known project is unknown; usage is what it holds. */
  #readRunHistory(
    request: Extract<
      HelperRequest,
      {
        type:
          | 'runs.read_events'
          | 'runs.events_for_span'
          | 'runs.read_tail'
          | 'runs.summary'
          | 'usage.window';
      }
    >,
  ): void {
    const id = request.request_id;
    if (!this.#helper.host.projects.has(request.project)) {
      this.#send({...this.#error(id, 'project_missing', 'no such project'), path: request.project});
      return;
    }
    if (request.type === 'usage.window') {
      this.#usage(request);
      return;
    }
    if (request.type === 'runs.summary') {
      const served = this.#helper.host.summaries
        .get(`${request.project} ${request.run_id}`)
        ?.find(entry => entry.offset === request.offset);
      if (served !== undefined) {
        this.#send({...served.page, type: 'runs.summary', request_id: id});
        return;
      }
    }
    this.#send({
      ...this.#error(id, 'unknown_run', `${request.project} has no run '${request.run_id}'`),
      path: request.project,
    });
  }

  #taskGoal(request: Extract<HelperRequest, {type: 'project.task_goal'}>): void {
    const id = request.request_id;
    const {host} = this.#helper;
    if (!host.projects.has(request.project)) {
      this.#send({...this.#error(id, 'project_missing', 'no such project'), path: request.project});
      return;
    }
    const goal = host.goals.get(`${request.project} ${request.task}`);
    if (goal === undefined) {
      this.#send({
        ...this.#error(id, 'task_invalid', `no task ${request.task} with a goal`),
        path: request.project,
      });
      return;
    }
    this.#send({type: 'project.task_goal', request_id: id, goal});
  }

  #project(
    request: Extract<
      HelperRequest,
      {type: 'tasks.list' | 'runs.list' | 'run.start' | 'project.task_goal'}
    >,
  ): void {
    if (request.type === 'project.task_goal') {
      this.#taskGoal(request);
      return;
    }
    const id = request.request_id;
    const helper = this.#helper;
    const tasks = helper.host.projects.get(request.project);
    if (tasks === undefined) {
      this.#send({...this.#error(id, 'project_missing', 'no such project'), path: request.project});
      return;
    }
    if (request.type === 'tasks.list') {
      this.#send({
        type: 'tasks.list',
        request_id: id,
        tasks: {version: 1, project_root: request.project, tasks: tasks.map(name => ({name}))},
      });
      return;
    }
    if (request.type === 'runs.list') {
      this.#send({
        type: 'runs.list',
        request_id: id,
        runs: {
          project_root: request.project,
          runs: (helper.host.runHistory.get(request.project) ?? []).map(run => {
            const {digest: _digest, attention: _attention, ...plain} = run;
            if (request.digest_runs?.includes(run.run_id) !== true) return plain;
            if (request.with_dashboard === true) return run;
            // A client that did not ask for the dashboard never sees it.
            const {attention: _unasked, ...asked} = run;
            if (asked.digest?.kind !== 'digest') return asked;
            const {dashboard: _dashboard, ...digest} = asked.digest;
            return {...asked, digest};
          }),
        },
      });
      return;
    }
    const resumed =
      request.resume != null && 'run_id' in request.resume ? request.resume.run_id : null;
    const driving =
      resumed === null ? undefined : helper.host.instances.find(entry => entry.run_id === resumed);
    if (driving !== undefined) {
      this.#send({
        type: 'run.start',
        request_id: id,
        launch: {
          version: 1,
          outcome: 'failed',
          code: 'run_already_live',
          stage: 'resolve',
          message: 'live',
          exit_code: 1,
          log_path: null,
          live_instance: driving,
        },
      });
      return;
    }
    const live = record(
      helper.newInstanceId(),
      resumed ?? `run-${helper.spawned}`,
      request.project,
    );
    helper.host.runs.set(live.id, ECHO_SERVER);
    helper.publish(live);
    this.#send({type: 'run.start', request_id: id, launch: live});
  }

  #runRequest(
    request: Extract<HelperRequest, {type: 'run.stop' | 'run.reader' | 'run.connect'}>,
  ): void {
    if (request.type === 'run.stop') this.#stop(request);
    else if (request.type === 'run.reader') this.#reader(request);
    else this.#connect(request.request_id, request.instance_id);
  }

  /** `run.reader`: a live run's own server, else the read-only one a run that ended shares. */
  #reader(request: Extract<HelperRequest, {type: 'run.reader'}>): void {
    const helper = this.#helper;
    const id = request.request_id;
    const live = helper.host.instances.find(entry => entry.run_id === request.run_id);
    if (live !== undefined) {
      this.#send({type: 'run.reader', request_id: id, instance_id: live.id, read_only: false});
      return;
    }
    const known = helper.host.runHistory.get(request.project) ?? [];
    if (!known.some(run => run.run_id === request.run_id)) {
      this.#send(this.#error(id, 'unknown_run', `${request.project} has no run ${request.run_id}`));
      return;
    }
    let reader = helper.host.readers.get(request.run_id);
    if (reader === undefined) {
      reader = helper.newInstanceId();
      helper.host.readers.set(request.run_id, reader);
      helper.host.readerStarts.push(request.run_id);
    }
    this.#send({type: 'run.reader', request_id: id, instance_id: reader, read_only: true});
  }

  #connect(id: string, instanceId: string): void {
    const helper = this.#helper;
    const reader = [...helper.host.readers].find(([, instance]) => instance === instanceId);
    const live =
      helper.host.instances.find(entry => entry.id === instanceId) ??
      (reader === undefined ? undefined : record(instanceId, reader[0]));
    const server =
      helper.host.runs.get(instanceId) ??
      (reader === undefined ? undefined : helper.host.readerServer);
    if (this.#watches.size > 0) {
      this.#send(this.#error(id, 'channel_busy', 'busy'));
      return;
    }
    if (live === undefined || server === undefined) {
      this.#send(this.#error(id, 'run_gone', `no live server has id ${instanceId}`));
      return;
    }
    if (helper.host.denied.has(instanceId)) {
      this.#send(this.#error(id, 'connect_denied', `run ${instanceId} is not yours`));
      return;
    }
    const greeting = helper.runGreeting === null ? '' : `${helper.runGreeting}\n`;
    this.#send(
      {type: 'run.connect', request_id: id, run_id: live.run_id, protocol_version: 1},
      greeting,
    );
    const listeners: ((line: string) => void)[] = [];
    this.#relay = line => {
      for (const listener of listeners) listener(line);
    };
    server({
      write: line => this.#channel.stdout.write(`${line}\n`),
      end: (outcome = 'server_closed') =>
        this.#close(3, `${JSON.stringify({outcome, exit_status: 3, detail: 'ended'})}\n`),
      onLine: listener => listeners.push(listener),
    });
  }

  #complete(id: string, prefix: string): void {
    const slash = prefix.lastIndexOf('/');
    const directory = slash <= 0 ? '/' : prefix.slice(0, slash);
    const typed = prefix.slice(slash + 1);
    const names = this.#helper.host.directories.get(directory);
    if (names === undefined) {
      this.#send({...this.#error(id, 'path_missing', 'missing'), path: directory});
      return;
    }
    const entries: PathEntry[] = names
      .filter(name => name.startsWith(typed))
      .sort()
      .map(name => ({
        name,
        path: `${directory === '/' ? '' : directory}/${name}`,
        vibesys_checkout: false,
        vibesys_project: this.#helper.host.projects.has(`${directory}/${name}`),
        git_repository: false,
      }));
    this.#send({type: 'paths.complete', request_id: id, directory, entries, truncated: false});
  }
}

function requestIdOf(line: string): string | null {
  try {
    const value = JSON.parse(line) as {request_id?: unknown};
    return typeof value.request_id === 'string' ? value.request_id : null;
  } catch {
    return null;
  }
}

/** How a `FakeLink` fails to come up. */
export type LinkFault = {readonly cause: Cause; readonly detail: string} | null;

/** One explicitly deferred link establishment, released by a deterministic test action. */
export interface DeferredEstablish {
  /** Resolves when `establish` reaches this deferred attempt. */
  readonly received: Promise<void>;
  /** Resolves when its caller abandons the attempt. */
  readonly aborted: Promise<void>;
  /** Whether its caller has abandoned the attempt. */
  readonly abandoned: boolean;
  release(): void;
}

class PendingEstablish implements DeferredEstablish {
  readonly received: Promise<void>;
  readonly aborted: Promise<void>;
  readonly #received: () => void;
  readonly #aborted: () => void;
  #release: (() => void) | null = null;
  #abandoned = false;

  constructor() {
    let received = (): void => {};
    let aborted = (): void => {};
    this.received = new Promise(resolve => {
      received = resolve;
    });
    this.aborted = new Promise(resolve => {
      aborted = resolve;
    });
    this.#received = received;
    this.#aborted = aborted;
  }

  get abandoned(): boolean {
    return this.#abandoned;
  }

  wait(signal?: AbortSignal): Promise<void> {
    this.#received();
    return new Promise((resolve, reject) => {
      const abort = (): void => {
        this.#release = null;
        this.#abandoned = true;
        this.#aborted();
        reject(new ConnectionError('link', 'the connection attempt was abandoned'));
      };
      if (signal?.aborted === true) {
        abort();
        return;
      }
      signal?.addEventListener('abort', abort, {once: true});
      this.#release = () => {
        signal?.removeEventListener('abort', abort);
        resolve();
      };
    });
  }

  release(): void {
    const release = this.#release;
    this.#release = null;
    release?.();
  }
}

interface FakeEstablishAttempt {
  readonly controller: AbortController;
  readonly waiters: Set<object>;
  promise: Promise<void>;
}

/** The `Link` to a `FakeHelper`'s host. */
export class FakeLink implements Link {
  readonly host = 'fake';
  readonly helper: FakeHelper;
  /** When set, `establish` rejects with it (an interactive attempt included, unless `signIn` clears it). */
  linkFault: LinkFault = null;
  /** Interactive sign-ins asked for. */
  interactive = 0;
  /** Establish calls made, including attempts a caller later abandoned. */
  establishments = 0;
  released = 0;
  readonly #establishes: PendingEstablish[] = [];
  #establishing: FakeEstablishAttempt | null = null;
  /** Every line a bootstrap reported, in order. */
  readonly heardLines: string[] = [];

  constructor(helper = new FakeHelper()) {
    this.helper = helper;
  }

  /** Hold the next `establish` until the test releases or its caller abandons it. */
  deferEstablish(): DeferredEstablish {
    const pending = new PendingEstablish();
    this.#establishes.push(pending);
    return pending;
  }

  establish(interactive: boolean, signal?: AbortSignal): Promise<void> {
    this.establishments += 1;
    if (interactive) this.interactive += 1;
    if (this.linkFault !== null) {
      return Promise.reject(new ConnectionError(this.linkFault.cause, this.linkFault.detail));
    }
    if (signal?.aborted === true) {
      return Promise.reject(new ConnectionError('link', 'the connection attempt was abandoned'));
    }
    const attempt = this.#establishing ?? this.#beginEstablish();
    const waiter = {};
    attempt.waiters.add(waiter);
    return new Promise((resolve, reject) => {
      const stopWaiting = (): boolean => {
        if (!attempt.waiters.delete(waiter)) return false;
        signal?.removeEventListener('abort', aborted);
        return true;
      };
      const aborted = (): void => {
        if (!stopWaiting()) return;
        reject(new ConnectionError('link', 'the connection attempt was abandoned'));
        if (attempt.waiters.size === 0) this.#cancelEstablish(attempt);
      };
      signal?.addEventListener('abort', aborted, {once: true});
      void attempt.promise.then(
        () => {
          if (stopWaiting()) resolve();
        },
        error => {
          if (stopWaiting()) reject(error);
        },
      );
    });
  }

  #beginEstablish(): FakeEstablishAttempt {
    const controller = new AbortController();
    const attempt: FakeEstablishAttempt = {
      controller,
      waiters: new Set(),
      promise: Promise.resolve(),
    };
    this.#establishing = attempt;
    attempt.promise = (
      this.#establishes.shift()?.wait(controller.signal) ?? Promise.resolve()
    ).finally(() => {
      if (this.#establishing === attempt) this.#establishing = null;
    });
    return attempt;
  }

  #cancelEstablish(attempt: FakeEstablishAttempt): void {
    if (this.#establishing === attempt) this.#establishing = null;
    attempt.controller.abort();
  }

  networkChanged(): void {
    if (this.#establishing !== null) this.#cancelEstablish(this.#establishing);
  }

  spawnHelper(checkout: string): SpawnedProcess {
    if (this.linkFault !== null) {
      const channel = fakeProcess();
      channel.end(255, '', 'ssh: connect to host fake: Network is unreachable\n');
      return channel.process;
    }
    return this.helper.spawn(checkout);
  }

  heard(line: string): void {
    this.heardLines.push(line);
  }

  explain(code: number | null, stderr: string, where: string): ConnectionError {
    if (this.linkFault !== null)
      return new ConnectionError(this.linkFault.cause, this.linkFault.detail);
    if (code === 255 || code === null)
      return new ConnectionError('link', stderr || 'the channel broke');
    return new ConnectionError('incompatible', `the helper for ${where} exited with ${code}`);
  }

  release(): Promise<void> {
    return this.#release();
  }

  async #release(): Promise<void> {
    this.released += 1;
    const attempt = this.#establishing;
    if (attempt === null) return;
    this.#cancelEstablish(attempt);
    await attempt.promise.catch(() => {});
  }
}
