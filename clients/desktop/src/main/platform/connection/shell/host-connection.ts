/**
 * `HostConnection`: the thin shell over one host's connection machine (`../state/host-connection.ts`).
 *
 * It owns its long-lived control channel and every run channel it opened, each registered with one
 * `AsyncDisposableStack` and one `AbortSignal`: closing the connection aborts what is in flight and
 * releases everything once (timers and requests, then the channels). The link itself (an ssh
 * master) is shared by every connection to the host, so its owner is the `ConnectionManager`. It performs the machine's requests (link, `hello`, `ping`,
 * and the check that is the failing operation) and feeds their typed outcomes back as events. It
 * makes no lifecycle decision of its own.
 *
 * Callers never retry: `request` and `openRun` wait until the host is ready, and reject with the
 * machine's terminal reason (offline, auth-needed, incompatible, closed) instead.
 */
import type {ScheduleTimeout} from '@vibesys/backend-client';
import type {InstanceList} from '@vibesys/backend-client/host-helper';
import {
  type Cause,
  type ConnectionConfig,
  type ConnectionEvent,
  type ConnectionRequest,
  type ConnectionState,
  INITIAL,
  type Operation,
  type Phase,
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
  readonly appVersion: string;
  readonly config: ConnectionConfig;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly log: (message: string) => void;
}

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

export class HostConnection implements AsyncDisposable {
  readonly #options: HostConnectionOptions;
  readonly #stack = new AsyncDisposableStack();
  readonly #abort = new AbortController();
  readonly #timers = new Map<number, () => void>();
  readonly #runs = new Set<HelperChannel>();
  readonly #phaseListeners = new Set<(phase: Phase) => void>();
  readonly #unreachable = new Set<(instanceId: string, detail: string) => void>();
  readonly #watchers = new Set<(listing: InstanceList) => void>();
  readonly #waiters = new Set<{resolve: () => void; reject: (error: Error) => void}>();
  #state: ConnectionState = INITIAL;
  #control: HelperChannel | null = null;
  #listing: InstanceList | null = null;

  constructor(options: HostConnectionOptions) {
    this.#options = options;
    // Released in reverse: the channels after what is in flight was aborted.
    this.#stack.defer(async () => {
      const channels = [...this.#runs, ...(this.#control === null ? [] : [this.#control])];
      this.#runs.clear();
      this.#control = null;
      await Promise.all(channels.map(channel => channel[Symbol.asyncDispose]()));
    });
    this.#stack.defer(() => {
      this.#abort.abort();
      for (const cancel of this.#timers.values()) cancel();
      this.#timers.clear();
    });
  }

  get phase(): Phase {
    return this.#state.phase;
  }

  /** The checkout the control channel's helper runs from, once it answered `hello`. */
  get checkoutRoot(): string | null {
    return this.#control?.hello?.checkout?.root ?? null;
  }

  /** Feed the machine an event from outside: a wake, a network change, the user's retry. */
  dispatch(event: ConnectionEvent): void {
    const {state, requests} = step(this.#state, event, this.#options.config);
    // A heartbeat re-enters ready: only a phase that reads differently is a change.
    const changed = JSON.stringify(state.phase) !== JSON.stringify(this.#state.phase);
    this.#state = state;
    for (const request of requests) this.#run(request);
    if (changed) this.#phaseChanged(state.phase);
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
    const terminal = terminalError(this.#state.phase);
    if (terminal !== null) return Promise.reject(terminal);
    return new Promise((resolve, reject) => this.#waiters.add({resolve, reject}));
  }

  /** Send `body` on the control channel once the host is ready. */
  async request<T extends string>(body: Body & {readonly type: T}): Promise<ReplyOf<T>> {
    await this.ready();
    const control = this.#control;
    if (control === null) throw new ConnectionError('link', 'the control channel is down');
    return (await control.request(body)) as ReplyOf<T>;
  }

  /**
   * A run channel to `instanceId`, relaying the run protocol, with the run id the helper reported.
   * A dial the link broke is reported to the machine, and tried again once the host is ready; a
   * stream that later ends abnormally is reported too. Rejects with `run-gone`, `denied`, or the
   * host's terminal reason.
   */
  async openRun(instanceId: string): Promise<{runId: string; stream: RunStream}> {
    for (;;) {
      await this.ready();
      try {
        return await this.#connectRun(instanceId);
      } catch (error) {
        const failure = asConnectionError(error);
        if (failure.reason !== 'link') throw failure;
        this.dispatch({type: 'run-ended', instanceId, cause: 'link', detail: failure.message});
      }
    }
  }

  async [Symbol.asyncDispose](): Promise<void> {
    this.dispatch({type: 'close'});
    await this.#stack.disposeAsync();
  }

  async #connectRun(instanceId: string): Promise<{runId: string; stream: RunStream}> {
    const channel = await this.#openChannel();
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

  #openChannel(): Promise<HelperChannel> {
    const {link, appVersion, config, scheduleTimeout} = this.#options;
    return HelperChannel.open(link.spawnHelper(), {
      where: link.where,
      appVersion,
      deadlineMs: config.deadlineMs,
      scheduleTimeout,
      signal: this.#abort.signal,
      explain: (code, stderr) => link.explain(code, stderr),
    });
  }

  /** Open the control channel and its registry watch; its end is reported to the machine. */
  async #openControl(): Promise<void> {
    const channel = await this.#openChannel();
    if (this.#abort.signal.aborted) {
      await channel[Symbol.asyncDispose]();
      throw new ConnectionError('link', 'the connection was closed');
    }
    const previous = this.#control;
    this.#control = channel;
    if (previous !== null) void previous[Symbol.asyncDispose]();
    channel.onPush(push => {
      this.#listing = push.listing;
      for (const watcher of this.#watchers) watcher(push.listing);
    });
    void channel.ended.then(failure => {
      if (this.#control !== channel) return;
      this.#control = null;
      const cause: Cause = failure?.reason ?? 'link';
      this.dispatch({type: 'control-ended', cause, detail: failure?.message ?? 'the helper ended'});
    });
    if (channel.hello?.features.includes('instances_watch') === true) {
      await channel.request({type: 'instances.watch'});
    }
  }

  async #perform(operation: Operation): Promise<void> {
    const {link} = this.#options;
    switch (operation.kind) {
      case 'link':
        await link.establish(operation.interactive);
        return;
      case 'handshake':
        await this.#openControl();
        return;
      case 'ping':
        await this.#pingControl();
        return;
      case 'check':
        await link.establish(false);
        if (this.#control === null) await this.#openControl();
        await this.#pingControl();
        if (operation.target.kind === 'run') {
          // The check is the operation that failed: a fresh run.connect to the same run.
          const {stream} = await this.#connectRun(operation.target.instanceId);
          stream.destroy();
        }
        return;
    }
  }

  async #pingControl(): Promise<void> {
    const control = this.#control;
    if (control === null) throw new ConnectionError('link', 'the control channel is down');
    await control.request({type: 'ping'});
  }

  #run(request: ConnectionRequest): void {
    switch (request.type) {
      case 'perform': {
        const op = request.op;
        void this.#perform(request.operation).then(
          () => this.dispatch({type: 'succeeded', op}),
          (error: unknown) => {
            const failure = asConnectionError(error);
            this.dispatch({type: 'failed', op, cause: failure.reason, detail: failure.message});
          },
        );
        return;
      }
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
        void this.#stack.disposeAsync();
        return;
    }
  }

  #phaseChanged(phase: Phase): void {
    const detail = 'detail' in phase ? `: ${phase.detail}` : '';
    this.#options.log(`${this.#options.link.where}: ${phase.kind}${detail}`);
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
