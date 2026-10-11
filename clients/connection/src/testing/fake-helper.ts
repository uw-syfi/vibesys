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
  type HelperRequest,
  type InstanceList,
  type LiveInstanceRecord,
  type PathEntry,
  parseHelperRequest,
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
  /** Project directories and their tasks. */
  readonly projects: Map<string, readonly string[]>;
  /** Directories and the directories in them. */
  readonly directories: Map<string, readonly string[]>;
  /** How each live run answers its run channel. */
  readonly runs: Map<string, RunServer>;
  /** Live runs this user may not connect to (`connect_denied`). */
  readonly denied: Set<string>;
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
    projects: new Map(),
    directories: new Map(),
    runs: new Map(),
    denied: new Set(),
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
  /** Every channel ever started, in order. */
  readonly channels: FakeProcess[] = [];
  /** How many helpers were started: one per channel. */
  spawned = 0;
  /** Stop requests received, in order. */
  readonly stops: {readonly instanceId: string; readonly force: boolean}[] = [];
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
    this.host.instances.push(live);
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
      unreadable: [],
      unverified: [],
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

  #send(reply: Reply): void {
    const send = (): void => {
      if (!this.#channel.stdout.writableEnded)
        this.#channel.stdout.write(`${JSON.stringify(reply)}\n`);
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
    const helper = this.#helper;
    switch (request.type) {
      case 'hello': {
        const [low, high] = request.client_versions as [number, number];
        if (this.#features !== null || low > 1 || high < 1) {
          this.#send({
            ...this.#error(id, 'incompatible', 'versions'),
            supported_versions: [1, 1],
            vibesys_version: '0.1.1',
          });
          return;
        }
        this.#features = (request.features ?? []).filter(
          f => f === 'instances_watch' || f === 'paths_complete',
        );
        this.#send({
          type: 'hello',
          request_id: id,
          protocol_version: 1,
          features: this.#features,
          vibesys_version: '0.1.1',
          checkout: {root: '/src/vibesys', commit: null, dirty: null},
        });
        return;
      }
      case 'ping':
        this.#send({type: 'ping', request_id: id});
        return;
      case 'instances.list':
        this.#send({type: 'instances.list', request_id: id, listing: helper.listing()});
        return;
      case 'instances.watch': {
        if (!this.#negotiated('instances_watch', id)) return;
        this.#send({type: 'instances.watch', request_id: id, watch_id: id});
        const push = (): void =>
          this.#send({type: 'instances.changed', watch_id: id, listing: helper.listing()});
        this.#watches.set(id, helper.watch(push));
        push();
        return;
      }
      case 'instances.unwatch': {
        const stop = this.#watches.get(request.watch_id);
        if (stop === undefined) {
          this.#send(this.#error(id, 'unknown_watch', 'no such watch'));
          return;
        }
        stop();
        this.#watches.delete(request.watch_id);
        this.#send({type: 'instances.unwatch', request_id: id, watch_id: request.watch_id});
        return;
      }
      case 'tasks.list':
      case 'runs.list':
      case 'run.start':
        this.#project(request);
        return;
      case 'run.stop': {
        const live = helper.stop(request.instance_id, request.force ?? false);
        this.#send({
          type: 'run.stop',
          request_id: id,
          result: {
            version: 1,
            id: request.instance_id,
            outcome: live ? 'stopped' : 'not_running',
            route: null,
          },
        });
        return;
      }
      case 'run.connect':
        this.#connect(id, request.instance_id);
        return;
      case 'paths.complete':
        if (this.#negotiated('paths_complete', id)) this.#complete(id, request.prefix);
        return;
    }
  }

  #negotiated(feature: string, id: string): boolean {
    if (this.#features?.includes(feature) === true) return true;
    this.#send(this.#error(id, 'feature_not_negotiated', feature));
    return false;
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
    this.#send({type: 'run.connect', request_id: id, run_id: live.run_id, protocol_version: 1});
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

/** The `Link` to a `FakeHelper`'s host. */
export class FakeLink implements Link {
  readonly host = 'fake';
  readonly helper: FakeHelper;
  /** When set, `establish` rejects with it (an interactive attempt included, unless `signIn` clears it). */
  linkFault: LinkFault = null;
  /** Interactive sign-ins asked for. */
  interactive = 0;
  released = 0;
  /** Every line a bootstrap reported, in order. */
  readonly heardLines: string[] = [];

  constructor(helper = new FakeHelper()) {
    this.helper = helper;
  }

  async establish(interactive: boolean): Promise<void> {
    if (interactive) this.interactive += 1;
    if (this.linkFault !== null)
      throw new ConnectionError(this.linkFault.cause, this.linkFault.detail);
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

  async release(): Promise<void> {
    this.released += 1;
  }
}
