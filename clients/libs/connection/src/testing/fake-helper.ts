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
  type ChannelEndOutcome,
  type HarnessOption,
  type HelperFeature,
  type HelperRequest,
  helperFeaturesForRequest,
  type InstanceList,
  type LiveInstanceRecord,
  type PathEntry,
  parseHelperRequest,
  type UnreadableRecord,
} from '@vibesys/contracts/host-helper';
import {ConnectionError} from '../shell/helper-channel.js';
import type {Link} from '../shell/link.js';
import type {SpawnedProcess} from '../shell/process.js';
import type {Cause} from '../state/host-connection.js';
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
  /** Directories and the directories in them. */
  readonly directories: Map<string, readonly string[]>;
  /** How each live run answers its run channel. */
  readonly runs: Map<string, RunServer>;
  /** Live runs this user may not connect to (`connect_denied`). */
  readonly denied: Set<string>;
  /** Retained bounded diagnostic tails, by instance id. */
  readonly logs: Map<string, {readonly text: string; readonly truncated: boolean}>;
  /** The vetted provider and model pairs (`harnesses.list`), each provider's default first. */
  readonly harnesses: HarnessOption[];
}

/** The pairs a new `FakeHelper` offers: two providers, each default first. */
export const FAKE_HARNESSES: readonly HarnessOption[] = [
  {provider: 'codex', model: 'gpt-a', default: true},
  {provider: 'codex', model: 'gpt-b', default: false},
  {provider: 'claude', model: 'claude-a', default: true},
];

/**
 * The run options `server.host_api` names (`UNVETTED_MODEL_FLAG`, `CLI_PROVIDER_FLAG`,
 * `MODEL_FLAG`): the wire contract carries no constants, so this Fake repeats them.
 */
const UNVETTED_MODEL_FLAG = '--allow-unvetted-model';
const HARNESS_FLAGS = ['--cli-provider', '--model'] as const;

/** Whether argparse reads `token` (`--name` or `--name=value`) as `flag`, as the helper decides. */
function abbreviates(token: string, flag: string): boolean {
  const name = token.split('=', 1)[0] ?? '';
  return name.startsWith('--') && name.length > 2 && flag.startsWith(name);
}

/** Why the helper refuses `start`'s arguments, or null when it accepts them. */
function refusedArgument(start: Extract<HelperRequest, {type: 'run.start'}>): string | null {
  const refused =
    start.harness == null ? [UNVETTED_MODEL_FLAG] : [UNVETTED_MODEL_FLAG, ...HARNESS_FLAGS];
  for (const token of start.args ?? []) {
    const flag = refused.find(candidate => abbreviates(token, candidate));
    if (flag !== undefined) return `${token} selects ${flag}`;
  }
  return null;
}

export function record(id: string, runId: string, project = '/p'): LiveInstanceRecord {
  return {
    version: 4,
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
    directories: new Map(),
    runs: new Map(),
    denied: new Set(),
    logs: new Map(),
    harnesses: [...FAKE_HARNESSES],
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
  ]);
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
      version: 4,
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
        this.#project(request);
        return;
      case 'run.start':
        this.#start(request);
        return;
      case 'harnesses.list':
        this.#send({
          type: 'harnesses.list',
          request_id: id,
          harnesses: [...this.#helper.host.harnesses],
        });
        return;
      case 'run.stop':
        this.#stop(request);
        return;
      case 'run.connect':
        this.#connect(id, request.instance_id);
        return;
      case 'paths.complete':
        this.#complete(id, request.prefix);
        return;
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
    const harness = request.harness;
    if (harness != null) {
      const offered = this.#helper.host.harnesses;
      const vetted = offered.some(
        option => option.provider === harness.provider && option.model === harness.model,
      );
      if (!vetted) {
        const allowed = offered
          .filter(option => option.provider === harness.provider)
          .map(option => option.model);
        const models = allowed.length === 0 ? 'none' : allowed.join(', ');
        this.#send(this.#error(id, 'unvetted_harness', `vetted models: ${models}`));
        return;
      }
    }
    this.#project(request);
  }

  #project(
    request: Extract<HelperRequest, {type: 'tasks.list' | 'runs.list' | 'run.start'}>,
  ): void {
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
        runs: {project_root: request.project, runs: []},
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

  #connect(id: string, instanceId: string): void {
    const helper = this.#helper;
    const live = helper.host.instances.find(entry => entry.id === instanceId);
    const server = helper.host.runs.get(instanceId);
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
