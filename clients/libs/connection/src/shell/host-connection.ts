/**
 * `HostConnection`: the thin shell over one host's connection machine (`../state/host-connection.ts`).
 *
 * It owns its long-lived control channel, every run channel it opened, and every helper process it
 * started, all released by one `AsyncDisposableStack` under one `AbortSignal`: closing the
 * connection stops the machine (which stops what is in flight and cancels every timer) and resolves
 * once every channel's process has exited. The link itself (an ssh master) is shared by every
 * connection to the host, so its owner is the `ConnectionManager`. The connection provides the
 * machine's operations (link, `hello`, `ping`, and the check that is the failing operation) as
 * invoked actors, and its timers as the actor's clock. It makes no lifecycle decision of its own.
 *
 * A control channel an operation opens is that operation's until the machine accepts the outcome
 * and installs it (`installControl`); one the machine does not accept (`discard`) or stopped
 * waiting for (the operation's signal aborted) is closed here, never installed.
 *
 * Callers never retry: `request` and `openRun` wait until the host is ready, and reject with the
 * machine's terminal reason (offline, auth-needed, incompatible, closed) or with the failure itself.
 * A failed run dial is reported to the machine with its real cause, whose routing decides what that
 * means: a link-class cause starts a check that ends in ready or stops; `auth`/`incompatible` move
 * the host to its matching terminal phase; `run-gone`/`denied` are the run's own fact and leave the
 * host ready. A caller may cancel its own run dial without changing host state; the operation's
 * signal and the connection's lifetime jointly own any run channel that dial opened.
 */

import type {InstanceList} from '@vibesys/contracts/host-helper';
import {clockFrom} from '@vibesys/machine';
import {abortReason, type RunChannelRole, type ScheduleTimeout} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import type {LinkPoolDiagnostics} from '../state/channel-admission.js';
import {
  type Cause,
  type CheckTarget,
  type ConnectionConfig,
  type ConnectionEvent,
  connectionMachine,
  type Opened,
  type Phase,
  phaseOf,
} from '../state/host-connection.js';
import {
  type Body,
  ConnectionError,
  HelperChannel,
  type ReplyOf,
  type RunStream,
} from './helper-channel.js';
import {ChannelCapacityError, type Link} from './link.js';

export interface HostConnectionOptions {
  readonly link: Link;
  /** The VibeSys checkout on the host every helper of this connection runs from. */
  readonly checkout: string;
  readonly appVersion: string;
  readonly config: ConnectionConfig;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly log: (message: string) => void;
  /** Tell the owning manager after every channel and helper process has ended. */
  readonly released?: () => void;
}

/** What the app may tell a connection: a wake or a network change, or the user's retry. */
export type OutsideEvent = Extract<ConnectionEvent, {type: 'wake' | 'user-retry'}>;

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

/** Run `operation`, rejecting only with a `ConnectionError`, whose reason the machine decides on. */
async function typed<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    throw asConnectionError(error);
  }
}

/** Throw when `signal` aborted: the machine no longer waits for this operation. */
function stillWanted(signal: AbortSignal): void {
  if (signal.aborted) throw new ConnectionError('link', 'the operation was abandoned');
}

function samePhase(left: Phase, right: Phase): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export class HostConnection implements AsyncDisposable {
  /** `CHECKOUT on HOST`, for messages. */
  readonly where: string;
  readonly #options: HostConnectionOptions;
  readonly #stack = new AsyncDisposableStack();
  readonly #abort = new AbortController();
  readonly #actor: Actor<typeof connectionMachine>;
  readonly #runs = new Set<HelperChannel>();
  /** Control channels opened by an operation and not yet installed or closed. */
  readonly #opened = new Set<HelperChannel>();
  /** Helper handshakes that have not yet returned a channel to their owning operation. */
  readonly #openingChannels = new Set<Promise<HelperChannel>>();
  readonly #phaseListeners = new Set<(phase: Phase) => void>();
  readonly #unreachable = new Set<(instanceId: string, detail: string) => void>();
  readonly #watchers = new Set<(listing: InstanceList) => void>();
  readonly #waiters = new Set<{resolve: () => void; reject: (error: Error) => void}>();
  readonly #unheld = new Set<() => void>();
  /** The last listing each opened channel pushed, delivered once the machine installs it. */
  readonly #latest = new WeakMap<HelperChannel, InstanceList>();
  #phase: Phase = {kind: 'idle'};
  #control: HelperChannel | null = null;
  #listing: InstanceList | null = null;
  #closing: Promise<void> | null = null;
  #holds = 0;

  constructor(options: HostConnectionOptions) {
    this.#options = options;
    this.where = `${options.checkout} on ${options.link.host}`;
    const {link} = options;
    const machine = connectionMachine.provide({
      actors: {
        link: fromPromise(({input, signal}) =>
          typed(async () => {
            await link.establish(input.interactive, signal);
            stillWanted(signal);
          }),
        ),
        handshake: fromPromise(({signal}) => typed(() => this.#openControl(signal))),
        ping: fromPromise(() => typed(() => this.#pingControl())),
        check: fromPromise(({input, signal}) => typed(() => this.#check(input.target, signal))),
      },
      actions: {
        release: () => void this.#release(),
        installControl: (_, {opened}) => this.#install(opened),
        discard: (_, {opened}) => void this.#discard(opened),
      },
      guards: {
        current: (_, {opened}) => this.#current(opened),
      },
    });
    this.#actor = createActor(machine, {
      input: {config: options.config},
      clock: clockFrom(options.scheduleTimeout),
    });
    this.#actor.on('run-unreachable', ({instanceId, detail}) => {
      for (const listener of this.#unreachable) listener(instanceId, detail);
    });
    this.#actor.subscribe(snapshot => {
      const phase = phaseOf(snapshot);
      // A heartbeat re-enters ready: only a phase that reads differently is a change.
      if (samePhase(phase, this.#phase)) return;
      this.#phase = phase;
      this.#phaseChanged(phase);
    });
    // Released in reverse: the channels, and every process, after the machine stopped.
    this.#stack.defer(async () => {
      // Aborting the lifetime ends these handshakes. Join them before snapshotting the channel
      // sets, because an accepted outcome can move a channel into one of those sets as it settles.
      await Promise.all([...this.#openingChannels].map(opening => opening.catch(() => null)));
      const channels = [
        ...this.#runs,
        ...this.#opened,
        ...(this.#control === null ? [] : [this.#control]),
      ];
      this.#runs.clear();
      this.#opened.clear();
      this.#control = null;
      await Promise.all(channels.map(channel => channel[Symbol.asyncDispose]()));
    });
    // Stopping the machine stops every operation (aborting its signal) and cancels every timer.
    this.#stack.defer(() => {
      this.#abort.abort();
      this.#actor.stop();
    });
    this.#actor.start();
  }

  get phase(): Phase {
    return this.#phase;
  }

  /** Bounded role counts for connection inspection; the link exposes no transport configuration. */
  get poolDiagnostics(): LinkPoolDiagnostics {
    return this.#options.link.diagnostics();
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

  /** Tell the machine about a wake, a network change, or the user's retry. */
  dispatch(event: OutsideEvent): void {
    this.#send(event);
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

  /**
   * Resolve once the host is ready, opening the connection if it is idle. An owning operation may
   * withdraw its wait without changing the host connection's own lifetime.
   */
  ready(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted === true) return Promise.reject(abortReason(signal));
    if (this.#phase.kind === 'idle') this.#send({type: 'open'});
    if (this.#phase.kind === 'ready') return Promise.resolve();
    const terminal = terminalError(this.#phase);
    if (terminal !== null) return Promise.reject(terminal);
    return new Promise((resolve, reject) => {
      const settled = (complete: () => void): void => {
        this.#waiters.delete(waiter);
        signal?.removeEventListener('abort', aborted);
        complete();
      };
      const waiter = {
        resolve: () => settled(resolve),
        reject: (error: Error) => settled(() => reject(error)),
      };
      const aborted = (): void => {
        if (signal !== undefined) waiter.reject(abortReason(signal));
      };
      this.#waiters.add(waiter);
      signal?.addEventListener('abort', aborted, {once: true});
      // The native signal cannot abort between the entry check and this synchronous registration,
      // but this also keeps the contract correct for structural AbortSignal implementations.
      if (signal?.aborted === true) aborted();
    });
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

  /**
   * A run channel to `instanceId`, relaying the run protocol, with the run id the helper reported:
   * one dial once the host is ready. A dial that fails is reported to the machine with its real
   * cause, same as a stream that later ends abnormally (`#connectRun`'s `error` listener): a
   * link-class cause is checked and returns to ready (which wakes the run's page) or stops;
   * `auth`/`incompatible` move the host to its matching terminal phase. Rejects with `run-gone`,
   * `denied`, `link`, the host's terminal reason, or the caller's abort reason when `signal` ends.
   */
  async openRun(
    instanceId: string,
    role: RunChannelRole,
    signal?: AbortSignal,
  ): Promise<{runId: string; stream: RunStream}> {
    await this.ready(signal);
    const lifetime =
      signal === undefined ? this.#abort.signal : AbortSignal.any([this.#abort.signal, signal]);
    try {
      if (lifetime.aborted) throw abortReason(lifetime);
      return await this.#connectRun(instanceId, role, lifetime);
    } catch (error) {
      // Losing the page that owns this dial is ordinary resource cleanup, not evidence that either
      // the run or its host failed. Preserve the caller's cancellation outside that taxonomy.
      if (signal?.aborted === true) throw abortReason(signal);
      if (error instanceof ChannelCapacityError) throw error;
      const failure = asConnectionError(error);
      this.#send({type: 'run-ended', instanceId, cause: failure.reason, detail: failure.message});
      throw failure;
    }
  }

  /** Close for good; resolves once every channel this connection started has ended. */
  async [Symbol.asyncDispose](): Promise<void> {
    this.#send({type: 'close'});
    await this.#release();
  }

  #send(event: ConnectionEvent): void {
    this.#actor.send(event);
  }

  /** Release everything, once; every caller waits for the same release. */
  #release(): Promise<void> {
    this.#closing ??= this.#stack.disposeAsync().finally(() => this.#options.released?.());
    return this.#closing;
  }

  /** Write one request to the already-ready control channel. */
  #request<T extends string>(body: Body & {readonly type: T}): Promise<ReplyOf<T>> {
    const control = this.#control;
    if (control === null) throw new ConnectionError('link', 'the control channel is down');
    return control.request(body) as Promise<ReplyOf<T>>;
  }

  async #connectRun(
    instanceId: string,
    role: RunChannelRole,
    signal: AbortSignal,
  ): Promise<{runId: string; stream: RunStream}> {
    const channel = await this.#openChannel(role, signal);
    this.#runs.add(channel);
    void channel.ended.then(() => this.#runs.delete(channel));
    let connected: {runId: string; stream: RunStream};
    try {
      connected = await channel.connectRun(instanceId, role);
    } catch (error) {
      await channel[Symbol.asyncDispose]();
      throw error;
    }
    connected.stream.once('error', error => {
      if (signal.aborted) return;
      const cause: Cause = error instanceof ConnectionError ? error.reason : 'link';
      this.#send({type: 'run-ended', instanceId, cause, detail: error.message});
    });
    return connected;
  }

  /**
   * Start a helper and its channel, living until `signal` aborts; aborting `opening` before it
   * answered ends it too. Nothing starts once the connection is closing.
   */
  #openChannel(
    role: 'host-control' | RunChannelRole,
    signal: AbortSignal,
    opening?: AbortSignal,
  ): Promise<HelperChannel> {
    const {link, checkout, appVersion, config, scheduleTimeout} = this.#options;
    if (this.#abort.signal.aborted) {
      return Promise.reject(new ConnectionError('link', 'the connection was closed'));
    }
    const admissionSignal = opening === undefined ? signal : AbortSignal.any([signal, opening]);
    const channel = link.withHelper(checkout, role, admissionSignal, process =>
      HelperChannel.open(
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
      ),
    );
    this.#openingChannels.add(channel);
    void channel.then(
      () => this.#openingChannels.delete(channel),
      () => this.#openingChannels.delete(channel),
    );
    return channel;
  }

  /**
   * Open a control channel and its registry watch for the machine to install. It is the
   * operation's own resource until then: when the machine stops waiting (`signal` aborts) before
   * installing it, it is closed here.
   */
  async #openControl(signal: AbortSignal): Promise<HelperChannel> {
    const channel = await this.#openChannel('host-control', this.#abort.signal, signal);
    this.#opened.add(channel);
    signal.addEventListener('abort', () => void this.#discard(channel), {once: true});
    // Pushes reach watchers only once the machine installed this channel (`#install`).
    channel.onPush(push => {
      this.#latest.set(channel, push.listing);
      if (this.#control !== channel) return;
      this.#listing = push.listing;
      for (const watcher of this.#watchers) watcher(push.listing);
    });
    try {
      stillWanted(signal);
      if (channel.supportsRequest('instances.watch')) {
        await channel.request({type: 'instances.watch'});
      } else {
        const {listing} = await channel.request({type: 'instances.list'});
        this.#latest.set(channel, listing);
      }
      stillWanted(signal);
      if (!channel.active)
        throw new ConnectionError('link', 'the helper ended during control setup');
    } catch (error) {
      // The operation fails only once its helper is gone, so a retry never overlaps it.
      await this.#discard(channel);
      throw error;
    }
    return channel;
  }

  /** Whether the channel an outcome opened (or, when it opened none, the control) still serves. */
  #current(opened: Opened | undefined): boolean {
    const channel = (opened as HelperChannel | null | undefined) ?? this.#control;
    return channel?.active === true;
  }

  /** The machine accepted an operation that opened a control channel: it becomes the control. */
  #install(opened: Opened): void {
    if (opened === null) return;
    const channel = opened as HelperChannel;
    this.#opened.delete(channel);
    const previous = this.#control;
    this.#control = channel;
    if (previous !== null && previous !== channel) void previous[Symbol.asyncDispose]();
    const latest = this.#latest.get(channel);
    if (latest !== undefined) {
      this.#listing = latest;
      for (const watcher of this.#watchers) watcher(latest);
    }
    void channel.ended.then(failure => {
      if (this.#control !== channel) return;
      this.#control = null;
      const cause: Cause = failure?.reason ?? 'link';
      this.#send({type: 'control-ended', cause, detail: failure?.message ?? 'the helper ended'});
    });
  }

  /** Close a channel an operation opened that will not be installed; resolves once it ended. */
  async #discard(opened: Opened): Promise<void> {
    if (opened === null) return;
    const channel = opened as HelperChannel;
    if (!this.#opened.delete(channel)) return;
    await channel[Symbol.asyncDispose]();
  }

  async #check(target: CheckTarget, signal: AbortSignal): Promise<Opened> {
    await this.#options.link.establish(false, signal);
    stillWanted(signal);
    const opened = this.#control === null ? await this.#openControl(signal) : null;
    try {
      await this.#pingControl(opened ?? this.#control);
      stillWanted(signal);
      if (target.kind === 'run') {
        // The check is the operation that failed: a fresh run.connect to the same run.
        const lifetime = AbortSignal.any([this.#abort.signal, signal]);
        const {stream} = await this.#connectRun(target.instanceId, 'run-control', lifetime);
        stream.destroy();
      }
    } catch (error) {
      await this.#discard(opened);
      throw error;
    }
    return opened;
  }

  async #pingControl(channel = this.#control): Promise<void> {
    if (channel === null) throw new ConnectionError('link', 'the control channel is down');
    await channel.request({type: 'ping'});
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
