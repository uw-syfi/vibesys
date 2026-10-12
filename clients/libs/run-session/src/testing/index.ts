/**
 * Test support for the live session: an in-memory control transport whose event stream and
 * control channel fail independently, a page lifecycle the test drives, and `sessionWith`, which
 * builds a `WebSession` over them the way an app builds the real one. Reachable only from tests.
 */
import {
  BackendClientError,
  type ProtocolResponse,
  type RequestInput,
  type ServerMessage,
} from '@vibesys/contracts';
import {event, eventBatch, snapshotResponse} from '@vibesys/contracts/testing';
import {
  type ControlChannelState,
  type ControlTransport,
  type SubscribeOptions,
  sameControlChannelState,
} from '@vibesys/transport';
import {connectRun, type RunConnectionOptions} from '../connect.js';
import type {BrowserLifecycle, PageVisibility, TransportHooks} from '../ports.js';
import {WebSession} from '../web-session.js';

export type LifecycleEvent = 'visibilitychange' | 'online' | 'offline';

export class FakeLifecycle implements BrowserLifecycle {
  #visibilityState: PageVisibility = 'visible';
  #online = true;
  readonly #listeners: Record<LifecycleEvent, Set<() => void>> = {
    visibilitychange: new Set(),
    online: new Set(),
    offline: new Set(),
  };

  get visibilityState(): PageVisibility {
    return this.#visibilityState;
  }

  get online(): boolean {
    return this.#online;
  }

  addEventListener(type: LifecycleEvent, listener: () => void): void {
    this.#listeners[type].add(listener);
  }

  removeEventListener(type: LifecycleEvent, listener: () => void): void {
    this.#listeners[type].delete(listener);
  }

  setVisibility(state: PageVisibility): void {
    this.#visibilityState = state;
    this.emit('visibilitychange');
  }

  setOnline(online: boolean): void {
    this.#online = online;
    this.emit(online ? 'online' : 'offline');
  }

  emit(type: LifecycleEvent): void {
    for (const listener of this.#listeners[type]) listener();
  }

  listenerCount(type: LifecycleEvent): number {
    return this.#listeners[type].size;
  }
}

export interface SubscriptionRecord {
  readonly afterSequence: number;
  readonly options: SubscribeOptions | undefined;
  readonly onMessage: (message: ServerMessage) => void;
  readonly onDisconnect: (error: Error) => void;
  closed: boolean;
}

/**
 * An in-memory transport whose event stream and control channel fail
 * independently, the way the real one does: a gateway restart can leave the
 * transcript streaming while commands are undeliverable, and vice versa.
 */
export class FakeTransport implements ControlTransport {
  readonly requests: RequestInput[] = [];
  readonly subscriptions: SubscriptionRecord[] = [];
  readonly snapshots: Array<ProtocolResponse | Error | Promise<ProtocolResponse>> = [];
  readonly #subscriptionWaiters: Array<(subscription: SubscriptionRecord) => void> = [];
  readonly #subscriptionCloseWaiters: Array<(subscription: SubscriptionRecord) => void> = [];
  #deferNextSubscription = false;
  #releaseSubscription: (() => void) | null = null;
  /**
   * How many upcoming dials connect without delivering their bootstrap batch,
   * as a socket that dies between `subscribed` and the first batch does: the
   * gateway sends `SubscribedMessage` before `_write_bootstrap`, so a
   * subscription can be live with nothing folded under it.
   */
  silentDials = 0;
  closeCalls = 0;
  reconnectCalls = 0;
  readonly reconnectFailures: Error[] = [];
  readonly #hooks: TransportHooks;
  #controls: ControlChannelState = {status: 'connected'};

  constructor(hooks: TransportHooks) {
    this.#hooks = hooks;
  }

  /**
   * The control channel lost its connection and reported the outage.
   *
   * Deduplicated like the real `ControlChannel.#reportState`: a report is
   * emitted only when something a consumer renders actually changed. A Fake
   * that re-reported an unchanged state would let the session get away with
   * behavior the real channel never produces.
   */
  dropControlChannel(error: BackendClientError, retrying = false): void {
    this.#report({status: 'disconnected', error, everConnected: true, retrying});
  }

  /** The channel started a dial, so a reconnect affordance would no-op. */
  retryControlChannel(error: BackendClientError): void {
    this.dropControlChannel(error, true);
  }

  /** A redial brought the control channel back. */
  recoverControlChannel(): void {
    this.#report({status: 'connected'});
  }

  /**
   * Redial now. The real one cancels an armed redial and dials; this one
   * recovers the channel and reports it, so a test asserts the affordance had
   * an *effect* rather than that a method was called. An implementation that
   * dialed and discarded the recovery would pass a call count and fail this.
   */
  reconnect(): void {
    this.reconnectCalls += 1;
    const failure = this.reconnectFailures.shift();
    if (failure !== undefined) throw failure;
    this.recoverControlChannel();
  }

  #report(state: ControlChannelState): void {
    if (sameControlChannelState(this.#controls, state)) return;
    this.#controls = state;
    this.#hooks.onConnectionState(state);
  }

  async request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const response = this.snapshots.shift();
    if (response instanceof Error) throw response;
    return (await response) ?? snapshotResponse();
  }

  async subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<{close(): Promise<void>}> {
    const record: SubscriptionRecord = {
      afterSequence,
      options,
      onMessage,
      onDisconnect,
      closed: false,
    };
    this.subscriptions.push(record);
    this.#subscriptionWaiters.shift()?.(record);
    if (this.silentDials > 0) this.silentDials -= 1;
    else onMessage(streamBatch(`store-${this.subscriptions.length}`, afterSequence + 1));
    const subscription = {
      close: async () => {
        if (record.closed) return;
        record.closed = true;
        this.#subscriptionCloseWaiters.shift()?.(record);
      },
    };
    if (this.#deferNextSubscription) {
      this.#deferNextSubscription = false;
      await new Promise<void>(resolve => {
        this.#releaseSubscription = resolve;
      });
    }
    return subscription;
  }

  async close(): Promise<void> {
    this.closeCalls++;
  }

  /** Resolves when the stream makes its next dial. */
  nextSubscription(): Promise<SubscriptionRecord> {
    return new Promise(resolve => this.#subscriptionWaiters.push(resolve));
  }

  /** Resolves when the stream retires its next owned subscription. */
  nextSubscriptionClose(): Promise<SubscriptionRecord> {
    return new Promise(resolve => this.#subscriptionCloseWaiters.push(resolve));
  }

  /** Hold the next subscription handle until the test releases its handshake. */
  deferNextSubscribe(): void {
    this.#deferNextSubscription = true;
  }

  releasePendingSubscribe(): void {
    const release = this.#releaseSubscription;
    this.#releaseSubscription = null;
    release?.();
  }
}

/** A typed transport failure, which is all the channel ever reports. */
export function disconnect(message: string): BackendClientError {
  return new BackendClientError('disconnected', message);
}

/** The state a channel reports after losing a connection it had. */
export function lost(message: string, retrying = false): ControlChannelState {
  return {status: 'disconnected', error: disconnect(message), everConnected: true, retrying};
}

/** The state of a session whose stream and control channel are both healthy. */
export function healthy(): ReturnType<WebSession['getState']> {
  return {hasSnapshot: true, status: 'connected', error: null, controls: {status: 'connected'}};
}

/**
 * A session over an in-memory transport, built the way the session builds the
 * real one: the factory receives the session's observers, so the control
 * channel has somewhere to report.
 */
export function sessionWith(
  lifecycle: BrowserLifecycle,
  options: RunConnectionOptions = {},
): {
  readonly session: WebSession;
  readonly transport: FakeTransport;
} {
  const built: FakeTransport[] = [];
  const session = new WebSession({
    lifecycle,
    connect: connectRun(
      hooks => {
        const transport = new FakeTransport(hooks);
        built.push(transport);
        return transport;
      },
      {
        reconnectDelaysMs: options.reconnectDelaysMs ?? [0],
        ...(options.scheduleTimeout === undefined
          ? {}
          : {scheduleTimeout: options.scheduleTimeout}),
      },
    ),
  });
  const transport = built[0];
  if (transport === undefined) throw new Error('WebSession did not build its transport');
  return {session, transport};
}

export function streamBatch(
  storeId: string,
  sequence: number,
  historyAfterSequence = 0,
): ServerMessage {
  return eventBatch([event(sequence, 'server_ready')], {
    through_sequence: sequence,
    store_id: storeId,
    history_after_sequence: historyAfterSequence,
  });
}

export function outputBatch(storeId: string, sequence: number, content: string): ServerMessage {
  return eventBatch([event(sequence, 'agent_output_chunk', content)], {
    through_sequence: sequence,
    store_id: storeId,
    history_after_sequence: 0,
  });
}

export function waitForSession(session: WebSession, predicate: () => boolean): Promise<void> {
  if (predicate()) return Promise.resolve();
  return new Promise(resolve => {
    let unsubscribeSession = (): void => undefined;
    let unsubscribeStore = (): void => undefined;
    const observe = (): void => {
      if (!predicate()) return;
      unsubscribeSession();
      unsubscribeStore();
      resolve();
    };
    unsubscribeSession = session.subscribe(observe);
    unsubscribeStore = session.store.subscribe(observe);
  });
}

export function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let complete = (_value: T): void => {
    throw new Error('Deferred promise was not initialized');
  };
  const promise = new Promise<T>(resolve => {
    complete = resolve;
  });
  return {promise, resolve: complete};
}
