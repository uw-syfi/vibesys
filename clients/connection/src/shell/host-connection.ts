/**
 * `HostConnection`: the thin shell over one host's connection machine (`../state/host-connection.ts`).
 *
 * It owns its long-lived control channel, every run channel it opened, and every helper process it
 * started, all released by one `AsyncDisposableStack` under one `AbortSignal`: closing the
 * connection aborts what is in flight and resolves once every channel's process has exited. The
 * link itself (an ssh master) is shared by every connection to the host, so its owner is the
 * `ConnectionManager`. The connection performs the machine's requests (link, `hello`, `ping`, and
 * the check that is the failing operation), each under its own `AbortSignal` the machine can abort,
 * and feeds their typed outcomes back as events. It makes no lifecycle decision of its own.
 *
 * Callers never retry: `request` and `openRun` wait until the host is ready, and reject with the
 * machine's terminal reason (offline, auth-needed, incompatible, closed) or with the failure itself.
 * A run dial the link broke is reported to the machine, whose recovery ends in ready.
 */

import type {InstanceList} from '@vibesys/contracts/host-helper';
import type {ScheduleTimeout} from '@vibesys/transport';
import {
  type Cause,
  type ConnectionConfig,
  type ConnectionEvent,
  type ConnectionRequest,
  type ConnectionState,
  INITIAL,
  type Operation,
  type Phase,
  phaseOf,
  step,
} from '../state/host-connection.js';
import {
  type Body,
  ConnectionError,
  HelperChannel,
  type ReplyOf,
  type RunStream,
} from './helper-channel.js';
import type {Link} from './link.js';

export interface HostConnectionOptions {
  readonly link: Link;
  /** The VibeSys checkout on the host every helper of this connection runs from. */
  readonly checkout: string;
  readonly appVersion: string;
  readonly config: ConnectionConfig;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly log: (message: string) => void;
}

/** A guarded request's explicit write-boundary result. */
type GuardedRequestResult<T> =
  | {readonly kind: 'not-written'}
  | {readonly kind: 'answered'; readonly reply: T}
  | {readonly kind: 'failed'; readonly written: boolean; readonly error: unknown};

/** What a phase that ends every wait means for a caller. */
function terminalError(phase: Phase): ConnectionError | null {
  switch (phase.kind) {
    case 'offline':
      return new ConnectionError('link', phase.detail);
    case 'auth-needed':
      return new ConnectionError('auth', phase.detail);
    case 'incompatible':
      return new ConnectionError('incompatible', phase.detail);
    case 'closed':
      return new ConnectionError('link', 'the connection was closed');
    default:
      return null;
  }
}

function asConnectionError(error: unknown): ConnectionError {
  if (error instanceof ConnectionError) return error;
  return new ConnectionError('link', error instanceof Error ? error.message : String(error));
}

/** Throw when `signal` aborted: the machine no longer waits for this operation. */
function stillWanted(signal: AbortSignal): void {
  if (signal.aborted) throw new ConnectionError('link', 'the operation was abandoned');
}

export class HostConnection implements AsyncDisposable {
  /** `CHECKOUT on HOST`, for messages. */
  readonly where: string;
  readonly #options: HostConnectionOptions;
  readonly #stack = new AsyncDisposableStack();
  readonly #abort = new AbortController();
  readonly #timers = new Map<number, () => void>();
  /** The operations in flight, by id, each with what aborts it. */
  readonly #ops = new Map<number, AbortController>();
  readonly #runs = new Set<HelperChannel>();
  /** The exits of every helper process this connection started that has not exited yet. */
  readonly #exits = new Set<Promise<number | null>>();
  readonly #phaseListeners = new Set<(phase: Phase) => void>();
  readonly #unreachable = new Set<(instanceId: string, detail: string) => void>();
  readonly #watchers = new Set<(listing: InstanceList) => void>();
  readonly #waiters = new Set<{resolve: () => void; reject: (error: Error) => void}>();
  readonly #unheld = new Set<() => void>();
  #state: ConnectionState = INITIAL;
  #control: HelperChannel | null = null;
  #listing: InstanceList | null = null;
  #closing: Promise<void> | null = null;
  #holds = 0;

  constructor(options: HostConnectionOptions) {
    this.#options = options;
    this.where = `${options.checkout} on ${options.link.host}`;
    // Released in reverse: the channels, and every process, after what is in flight was aborted.
    this.#stack.defer(async () => {
      const channels = [...this.#runs, ...(this.#control === null ? [] : [this.#control])];
      this.#runs.clear();
      this.#control = null;
      await Promise.all(channels.map(channel => channel[Symbol.asyncDispose]()));
      await Promise.all(this.#exits);
    });
    this.#stack.defer(() => {
      this.#abort.abort();
      for (const op of this.#ops.values()) op.abort();
      this.#ops.clear();
      for (const cancel of this.#timers.values()) cancel();
      this.#timers.clear();
    });
  }

  get phase(): Phase {
    return phaseOf(this.#state);
  }

  /** The checkout the control channel's helper runs from, once it answered `hello`. */
  get checkoutRoot(): string | null {
    return this.#control?.hello?.checkout?.root ?? null;
  }

  /** True while something holds the connection open (`hold`). */
  get held(): boolean {
    return this.#holds > 0;
  }

  /**
   * Keep this connection open while a run is attached through it; returns the release (idempotent).
   * A connection its manager replaced closes once nothing holds it.
   */
  hold(): () => void {
    this.#holds += 1;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.#holds -= 1;
      if (this.#holds === 0) for (const listener of [...this.#unheld]) listener();
    };
  }

  /** Hear the last hold being released; returns the unsubscribe. */
  onUnheld(listener: () => void): () => void {
    this.#unheld.add(listener);
    return () => this.#unheld.delete(listener);
  }

  /** Feed the machine an event from outside: a wake, a network change, the user's retry. */
  dispatch(event: ConnectionEvent): void {
    const before = phaseOf(this.#state);
    const {state, requests} = step(this.#state, event, this.#options.config);
    const after = phaseOf(state);
    // A heartbeat re-enters ready: only a phase that reads differently is a change.
    const changed = JSON.stringify(after) !== JSON.stringify(before);
    this.#state = state;
    for (const request of requests) this.#run(request);
    if (changed) this.#phaseChanged(after);
  }

  /** Hear every phase change; returns the unsubscribe. */
  onPhase(listener: (phase: Phase) => void): () => void {
    this.#phaseListeners.add(listener);
    return () => this.#phaseListeners.delete(listener);
  }

  /** Hear a check that found a run gone or closed to this user; returns the unsubscribe. */
  onRunUnreachable(listener: (instanceId: string, detail: string) => void): () => void {
    this.#unreachable.add(listener);
    return () => this.#unreachable.delete(listener);
  }

  /**
   * Hear the host's registry as the helper pushes it (`instances.watch`): the latest listing at once
   * when there is one, then every change. Returns the unsubscribe.
   */
  watchInstances(listener: (listing: InstanceList) => void): () => void {
    this.#watchers.add(listener);
    if (this.#listing !== null) listener(this.#listing);
    else void this.ready().catch(() => {});
    return () => this.#watchers.delete(listener);
  }

  /** Resolve once the host is ready, opening the connection if it is idle. */
  ready(): Promise<void> {
    if (this.#state.phase.kind === 'idle') this.dispatch({type: 'open'});
    if (this.#state.phase.kind === 'ready') return Promise.resolve();
    const terminal = terminalError(this.phase);
    if (terminal !== null) return Promise.reject(terminal);
    return new Promise((resolve, reject) => this.#waiters.add({resolve, reject}));
  }

  /** Send `body` on the control channel once the host is ready. */
  async request<T extends string>(body: Body & {readonly type: T}): Promise<ReplyOf<T>> {
    await this.ready();
    return this.#request(body);
  }

  /**
   * Send `body` only if `current` still owns it after the host becomes ready. The predicate and
   * control-channel write share one synchronous boundary, so another main-process intent cannot
   * interleave between them. A failure says whether that write boundary was crossed.
   */
  async requestIfCurrent<T extends string>(
    body: Body & {readonly type: T},
    current: () => boolean,
  ): Promise<GuardedRequestResult<ReplyOf<T>>> {
    try {
      await this.ready();
    } catch (error) {
      return {kind: 'failed', written: false, error};
    }
    if (!current()) return {kind: 'not-written'};
    let reply: Promise<ReplyOf<T>>;
    try {
      reply = this.#request(body);
    } catch (error) {
      return {kind: 'failed', written: false, error};
    }
    try {
      return {kind: 'answered', reply: await reply};
    } catch (error) {
      return {kind: 'failed', written: true, error};
    }
  }

  /** Write one request to the already-ready control channel. */
  #request<T extends string>(body: Body & {readonly type: T}): Promise<ReplyOf<T>> {
    const control = this.#control;
    if (control === null) throw new ConnectionError('link', 'the control channel is down');
    return control.request(body) as Promise<ReplyOf<T>>;
  }

  /**
   * A run channel to `instanceId`, relaying the run protocol, with the run id the helper reported:
   * one dial once the host is ready. A dial the link broke is reported to the machine, which checks
   * the run and returns to ready (which wakes the run's page) or stops; a stream that later ends
   * abnormally is reported too. Rejects with `run-gone`, `denied`, `link`, or the host's terminal
   * reason.
   */
  async openRun(instanceId: string): Promise<{runId: string; stream: RunStream}> {
    await this.ready();
    try {
      return await this.#connectRun(instanceId, this.#abort.signal);
    } catch (error) {
      const failure = asConnectionError(error);
      if (failure.reason === 'link') {
        this.dispatch({type: 'run-ended', instanceId, cause: 'link', detail: failure.message});
      }
      throw failure;
    }
  }

  /** Close for good; resolves once every channel this connection started has ended. */
  async [Symbol.asyncDispose](): Promise<void> {
    this.dispatch({type: 'close'});
    await this.#release();
  }

  /** Release everything, once; every caller waits for the same release. */
  #release(): Promise<void> {
    this.#closing ??= this.#stack.disposeAsync();
    return this.#closing;
  }

  async #connectRun(
    instanceId: string,
    signal: AbortSignal,
  ): Promise<{runId: string; stream: RunStream}> {
    const channel = await this.#openChannel(signal);
    this.#runs.add(channel);
    void channel.ended.then(() => this.#runs.delete(channel));
    let connected: {runId: string; stream: RunStream};
    try {
      connected = await channel.connectRun(instanceId);
    } catch (error) {
      await channel[Symbol.asyncDispose]();
      throw error;
    }
    connected.stream.once('error', error => {
      const cause: Cause = error instanceof ConnectionError ? error.reason : 'link';
      this.dispatch({type: 'run-ended', instanceId, cause, detail: error.message});
    });
    return connected;
  }

  /**
   * Start a helper and its channel, living until `signal` aborts; aborting `opening` before it
   * answered ends it too. Nothing starts once the connection is closing.
   */
  #openChannel(signal: AbortSignal, opening?: AbortSignal): Promise<HelperChannel> {
    const {link, checkout, appVersion, config, scheduleTimeout} = this.#options;
    if (this.#abort.signal.aborted) {
      return Promise.reject(new ConnectionError('link', 'the connection was closed'));
    }
    const process = link.spawnHelper(checkout);
    this.#exits.add(process.exit);
    void process.exit.then(() => this.#exits.delete(process.exit));
    return HelperChannel.open(
      process,
      {
        where: this.where,
        appVersion,
        deadlineMs: config.deadlineMs,
        scheduleTimeout,
        signal,
        explain: (code, stderr) => link.explain(code, stderr, this.where),
        heard: line => link.heard(line),
      },
      opening,
    );
  }

  /**
   * Open the control channel and its registry watch; its end is reported to the machine. A channel
   * that answers after `signal` aborted (the machine stopped waiting) is closed, never installed.
   */
  async #openControl(signal: AbortSignal): Promise<HelperChannel> {
    const lifetime = AbortSignal.any([this.#abort.signal, signal]);
    const channel = await this.#openChannel(lifetime, signal);
    let published = false;
    let ended = false;
    channel.onPush(push => {
      this.#listing = push.listing;
      for (const watcher of this.#watchers) watcher(push.listing);
    });
    void channel.ended.then(failure => {
      ended = true;
      if (this.#control !== channel) return;
      this.#control = null;
      const cause: Cause = failure?.reason ?? 'link';
      this.dispatch({type: 'control-ended', cause, detail: failure?.message ?? 'the helper ended'});
    });
    try {
      stillWanted(signal);
      if (channel.hello?.features.includes('instances_watch') === true) {
        await channel.request({type: 'instances.watch'});
      }
      stillWanted(signal);
      if (ended || !channel.active) {
        throw new ConnectionError('link', 'the helper ended during control setup');
      }
      const previous = this.#control;
      this.#control = channel;
      published = true;
      if (previous !== null) void previous[Symbol.asyncDispose]();
    } finally {
      if (!published) await channel[Symbol.asyncDispose]();
    }
    return channel;
  }

  /**
   * Perform one machine operation and return its synchronous success check. No channel-end
   * continuation can interleave between that check and the machine accepting the outcome.
   */
  async #perform(operation: Operation, signal: AbortSignal): Promise<() => void> {
    const {link} = this.#options;
    switch (operation.kind) {
      case 'link':
        await link.establish(operation.interactive);
        return () => {};
      case 'handshake': {
        const control = await this.#openControl(signal);
        return () => this.#assertControl(control, signal);
      }
      case 'ping': {
        const control = await this.#pingControl();
        return () => this.#assertControl(control, signal);
      }
      case 'check': {
        await link.establish(false);
        stillWanted(signal);
        if (this.#control === null) await this.#openControl(signal);
        const control = await this.#pingControl();
        stillWanted(signal);
        if (operation.target.kind === 'run') {
          // The check is the operation that failed: a fresh run.connect to the same run.
          const lifetime = AbortSignal.any([this.#abort.signal, signal]);
          const {stream} = await this.#connectRun(operation.target.instanceId, lifetime);
          stream.destroy();
        }
        return () => this.#assertControl(control, signal);
      }
    }
  }

  /** Reject a stale operation outcome before the state machine can accept it as success. */
  #assertControl(control: HelperChannel, signal: AbortSignal): void {
    stillWanted(signal);
    if (this.#control !== control || !control.active) {
      throw new ConnectionError('link', 'the helper ended during the connection operation');
    }
  }

  async #pingControl(): Promise<HelperChannel> {
    const control = this.#control;
    if (control === null) throw new ConnectionError('link', 'the control channel is down');
    await control.request({type: 'ping'});
    return control;
  }

  #run(request: ConnectionRequest): void {
    switch (request.type) {
      case 'perform': {
        const {op} = request;
        const abort = new AbortController();
        this.#ops.set(op, abort);
        const failed = (error: unknown): void => {
          this.#ops.delete(op);
          const failure = asConnectionError(error);
          this.dispatch({type: 'failed', op, cause: failure.reason, detail: failure.message});
        };
        // An aborted operation's outcome still arrives; the machine ignores it by its id.
        void this.#perform(request.operation, abort.signal).then(accept => {
          try {
            accept();
            this.#ops.delete(op);
            this.dispatch({type: 'succeeded', op});
          } catch (error) {
            failed(error);
          }
        }, failed);
        return;
      }
      case 'abort':
        this.#ops.get(request.op)?.abort();
        this.#ops.delete(request.op);
        return;
      case 'schedule':
        this.#timers.set(
          request.timer,
          this.#options.scheduleTimeout(() => {
            this.#timers.delete(request.timer);
            this.dispatch({type: 'timer-fired', timer: request.timer});
          }, request.delayMs),
        );
        return;
      case 'cancel':
        this.#timers.get(request.timer)?.();
        this.#timers.delete(request.timer);
        return;
      case 'run-unreachable':
        for (const listener of this.#unreachable) listener(request.instanceId, request.detail);
        return;
      case 'release':
        void this.#release();
        return;
    }
  }

  #phaseChanged(phase: Phase): void {
    const detail = 'detail' in phase ? `: ${phase.detail}` : '';
    this.#options.log(`${this.where}: ${phase.kind}${detail}`);
    if (phase.kind === 'ready') {
      for (const waiter of this.#waiters) waiter.resolve();
      this.#waiters.clear();
    }
    const terminal = terminalError(phase);
    if (terminal !== null) {
      for (const waiter of this.#waiters) waiter.reject(terminal);
      this.#waiters.clear();
    }
    for (const listener of this.#phaseListeners) listener(phase);
  }
}
