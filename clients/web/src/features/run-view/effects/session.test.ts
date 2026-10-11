import {describe, expect, test} from 'bun:test';
import {
  BackendClientError,
  type BatchReconciliation,
  type ControlChannelState,
  type ControlTransport,
  type ProtocolResponse,
  type RequestInput,
  type RunSnapshot,
  type RunStatus,
  ServerError,
  type ServerMessage,
  type StreamMessageContext,
  type SubscribeOptions,
  sameControlChannelState,
} from '@vibesys/backend-client';
import {event, eventBatch, FakeClock, snapshotResponse} from '@vibesys/backend-client/testing';
import {SEEDS, Seeded} from '@vibesys/core-state/testing';
import {
  connectRun,
  type RunConnection,
  type RunStreamHandlers,
  type TransportHooks,
  type TransportTiming,
} from '../../../platform/backend.js';
import type {BrowserLifecycle} from '../../../platform/lifecycle.js';
import {connectionBanners, STREAM_BANNER_COPY} from '../state/banners.js';
import {WebSession} from './session.js';

type LifecycleEvent = 'visibilitychange' | 'online' | 'offline';

class FakeLifecycle implements BrowserLifecycle {
  #visibilityState: DocumentVisibilityState = 'visible';
  #online = true;
  readonly #listeners: Record<LifecycleEvent, Set<() => void>> = {
    visibilitychange: new Set(),
    online: new Set(),
    offline: new Set(),
  };

  get visibilityState(): DocumentVisibilityState {
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

  setVisibility(state: DocumentVisibilityState): void {
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

interface SubscriptionRecord {
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
class FakeTransport implements ControlTransport {
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

/** A connection whose replacement batch arrives before its subscription handle is adopted. */
class PendingHandleConnection implements RunConnection {
  readonly failure = new Error('replacement snapshot failed');
  #snapshot = 0;
  #release: (() => void) | null = null;

  loadSnapshot(): Promise<RunSnapshot> {
    this.#snapshot += 1;
    return this.#snapshot === 1
      ? Promise.reject(this.failure)
      : Promise.resolve({run_id: 'run-1', sequence: 1, status: 'running'});
  }

  subscribe(handlers: RunStreamHandlers): Promise<void> {
    const reconciliation: BatchReconciliation = {kind: 'rebootstrap', historyFloor: 0};
    const context: StreamMessageContext = {resumed: false, connectionGeneration: 1};
    handlers.onBatch(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
      }),
      reconciliation,
      context,
    );
    return new Promise(resolve => {
      this.#release = resolve;
    });
  }

  retry(): void {}
  retryIfDisconnected(): void {}
  rebootstrap(): 'current' {
    return 'current';
  }
  redialControls(): void {}
  close(): Promise<void> {
    return Promise.resolve();
  }

  release(): void {
    this.#release?.();
  }
}

/** A replacement that faults after connecting but before its fenced snapshot settles. */
class FaultBeforeSnapshotConnection implements RunConnection {
  readonly firstFailure = new Error('first fenced snapshot failed');
  readonly streamFault = new Error('replacement stream sent an invalid frame');
  readonly rebootstrapStarted: Promise<void>;
  #announceRebootstrap: () => void = () => undefined;
  #handlers: RunStreamHandlers | null = null;
  #snapshots = 0;
  #resolveSnapshot: (snapshot: RunSnapshot) => void = () => undefined;

  constructor() {
    this.rebootstrapStarted = new Promise(resolve => {
      this.#announceRebootstrap = resolve;
    });
  }

  loadSnapshot(): Promise<RunSnapshot> {
    this.#snapshots += 1;
    if (this.#snapshots === 1) return Promise.reject(this.firstFailure);
    return new Promise(resolve => {
      this.#resolveSnapshot = resolve;
    });
  }

  subscribe(handlers: RunStreamHandlers): Promise<void> {
    this.#handlers = handlers;
    this.#batch('store-a', 1);
    return Promise.resolve();
  }

  retry(): void {}
  retryIfDisconnected(): void {}
  rebootstrap(): 'transitioning' {
    this.#announceRebootstrap();
    return 'transitioning';
  }
  redialControls(): void {}
  close(): Promise<void> {
    return Promise.resolve();
  }

  establishFaultedReplacement(): void {
    this.#batch('store-b', 2);
    this.#handlers?.onConnectionState({status: 'connected'});
    this.#handlers?.onFault(this.streamFault);
  }

  resolveReplacementSnapshot(): void {
    this.#resolveSnapshot({run_id: 'run-1', sequence: 1, status: 'running'});
  }

  #batch(storeId: string, connectionGeneration: number): void {
    const reconciliation: BatchReconciliation = {kind: 'rebootstrap', historyFloor: 0};
    this.#handlers?.onBatch(
      eventBatch([event(1, 'server_ready')], {
        store_id: storeId,
        through_sequence: 1,
        history_after_sequence: 0,
      }),
      reconciliation,
      {resumed: false, connectionGeneration},
    );
  }
}

describe('WebSession', () => {
  test('keeps a newer terminal watermark when a stale same-run refresh is rejected', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));

    await session.start();
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');

    // Core-state rejects this same-run response's stale liveness: it cannot un-end a fold at the
    // same cursor. It must not lower the separate evidence that sequence 5 exists in this log.
    const stale = deferred<ProtocolResponse>();
    transport.snapshots.push(stale.promise);
    transport.silentDials = 1;
    lifecycle.emit('online');
    const refreshed = nextSessionChange(session);
    stale.resolve(snapshotResponse({sequence: 1, status: 'running'}));
    await refreshed;
    await Promise.resolve();

    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(initial.closed).toBe(true);
    expect(transport.subscriptions).toHaveLength(2);
    const resumed = transport.subscriptions[1];
    if (resumed === undefined) throw new Error('Lifecycle recovery did not resume the stream');
    expect(resumed).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    resumed.onDisconnect(disconnect('stream ended before the known tail'));
    expect(session.getState().status).toBe('stale');
    expect(scheduler.pendingDelays()).toEqual([0]);
    await session.close();
  });

  test('keeps the old watermark until a fresh batch proves the stream store changed', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();

    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});
    expect(session.getState().status).toBe('stale');

    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));
    transport.silentDials = 1;
    const redialed = transport.nextSubscription();
    session.reattach();
    const replacement = await redialed;
    expect(replacement).toMatchObject({afterSequence: 0, options: undefined});

    // A fresh connection is not necessarily a fresh store. A whole-backlog WebSocket bootstrap
    // may be split into complete event batches, so this first same-store batch can end before the
    // accepted snapshot watermark. Losing the connection here must retain that evidence and retry.
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    replacement.onMessage(streamBatch('store-1', 1));
    await connected;
    replacement.onDisconnect(disconnect('same store ended before sequence 5'));

    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(session.getState().status).toBe('stale');
    expect(scheduler.pendingDelays()).toEqual([0]);
    await session.close();
  });

  test('accepts a lower watermark after the stream store re-bootstraps', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));

    await session.start();
    const subscription = transport.subscriptions[0];
    if (subscription === undefined) throw new Error('Initial subscription did not open');
    const replacementSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(replacementSnapshot.promise);
    subscription.onMessage(
      eventBatch([event(1, 'server_ready')], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'connecting'});

    // The pre-rebootstrap sequence-5 snapshot cannot describe the replacement. Its own
    // post-rebootstrap sequence-1 snapshot is authoritative and may lower the watermark.
    const refreshed = nextSessionChange(session);
    replacementSnapshot.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await refreshed;

    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(subscription.closed).toBe(false);
    expect(transport.subscriptions).toHaveLength(1);
    subscription.onDisconnect(disconnect('replacement store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    await session.close();
  });

  test('does not assign a pre-bootstrap snapshot to the replacement store', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));

    await session.start();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
    });

    // Reattach establishes its stream store before issuing any snapshot query,
    // so neither an old-store nor an early new-store response can be guessed
    // into the generation from cross-socket delivery order.
    const postReplacement = deferred<ProtocolResponse>();
    transport.snapshots.push(postReplacement.promise);
    transport.silentDials = 1;
    const replacementDial = transport.nextSubscription();
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    session.reattach();
    const replacement = await replacementDial;
    expect(transport.requests).toHaveLength(1);
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await connected;
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});

    const validated = nextSessionChange(session);
    postReplacement.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await validated;
    replacement.onDisconnect(disconnect('replacement store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    await session.close();
  });

  test('retains post-replacement evidence without replacing a live stream', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});

    // The reverse cross-socket ordering has the same handoff: the query still
    // begins only after the replacement batch, fenced to its store.
    const postReplacement = deferred<ProtocolResponse>();
    transport.snapshots.push(postReplacement.promise);
    transport.silentDials = 1;
    const replacementDial = transport.nextSubscription();
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    session.reattach();
    const replacement = await replacementDial;
    expect(transport.requests).toHaveLength(1);
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await connected;
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });

    const snapshotApplied = nextSessionChange(session);
    postReplacement.resolve(snapshotResponse({sequence: 3, status: 'completed'}));
    await snapshotApplied;
    expect(replacement.closed).toBe(false);
    expect(transport.subscriptions).toHaveLength(2);

    // The stream can deliver the durable suffix itself. Higher snapshot
    // evidence must not proactively retire a healthy socket just to discover
    // whether it had closed.
    replacement.onMessage(outputBatch('replacement-store', 2, 'second\n'));
    replacement.onMessage(outputBatch('replacement-store', 3, 'third\n'));
    replacement.onDisconnect(disconnect('replacement store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    await session.close();
  });

  test('retries a terminal close when its post-replacement snapshot later proves a gap', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});

    const postReplacement = deferred<ProtocolResponse>();
    transport.snapshots.push(postReplacement.promise);
    transport.silentDials = 1;
    const replacementDial = transport.nextSubscription();
    const connected = waitForSession(session, () => session.getState().status === 'connected');
    session.reattach();
    const replacement = await replacementDial;
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    await connected;

    // Before the generation-fenced snapshot returns, the sequence-1 terminal fold is all the
    // client can prove. Its close is quiet. Higher evidence arriving afterward must wake the dead
    // stream immediately instead of waiting for another browser lifecycle event.
    replacement.onDisconnect(disconnect('replacement closed before snapshot'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);

    transport.silentDials = 1;
    const resumedDial = transport.nextSubscription();
    postReplacement.resolve(snapshotResponse({sequence: 3, status: 'completed'}));
    const resumed = await resumedDial;
    expect(replacement.closed).toBe(true);
    expect(resumed).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'replacement-store'},
    });

    resumed.onDisconnect(disconnect('replacement still ends before sequence 3'));
    expect(session.getState().status).toBe('stale');
    expect(scheduler.pendingDelays()).toEqual([0]);
    await session.close();
  });

  test('rebootstraps when the fenced store is replaced before snapshot acquisition', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    transport.subscriptions[0]?.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
    });

    const storeB = deferred<ProtocolResponse>();
    transport.snapshots.push(storeB.promise);
    transport.silentDials = 1;
    const bDial = transport.nextSubscription();
    session.reattach();
    const b = await bDial;
    b.onMessage(streamBatch('store-b', 1));
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'store-b',
    });

    const storeC = deferred<ProtocolResponse>();
    transport.snapshots.push(storeC.promise);
    transport.silentDials = 1;
    const cDial = transport.nextSubscription();
    storeB.resolve(storeReplacedResponse());
    const c = await cDial;
    c.onMessage(streamBatch('store-c', 1));
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'store-c',
    });

    const accepted = waitForSession(session, () => session.store.getState().status === 'completed');
    storeC.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await accepted;
    expect(b.closed).toBe(true);
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(transport.requests).toHaveLength(3);
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('drops an older fenced reply when the replacement batch arrives first', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();
    transport.subscriptions[0]?.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
    });

    const storeB = deferred<ProtocolResponse>();
    transport.snapshots.push(storeB.promise);
    transport.silentDials = 1;
    const bDial = transport.nextSubscription();
    session.reattach();
    const stream = await bDial;
    stream.onMessage(streamBatch('store-b', 1));

    const storeC = deferred<ProtocolResponse>();
    transport.snapshots.push(storeC.promise);
    stream.onMessage(streamBatch('store-c', 1));
    expect(transport.requests.slice(-2)).toMatchObject([
      {type: 'query.snapshot', expected_store_id: 'store-b'},
      {type: 'query.snapshot', expected_store_id: 'store-c'},
    ]);

    const accepted = nextSessionChange(session);
    storeC.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await accepted;
    storeB.resolve(storeReplacedResponse());
    await Promise.resolve();
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('keeps one generation monotone while its fresh subscription handle is pending', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 5, status: 'completed'}));
    await session.start();

    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');
    initial.onMessage({type: 'protocol_error', code: 'stream_failed', message: 'Replay failed'});

    // The first explicit recovery gets a fresh sequence-zero batch before its
    // subscription handle settles. That batch is already the visible generation.
    const replacementSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(replacementSnapshot.promise);
    transport.silentDials = 1;
    transport.deferNextSubscribe();
    const replacementDial = transport.nextSubscription();
    session.reattach();
    const replacement = await replacementDial;
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'replacement-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'completed'});
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });

    // Repeated Reattach requests share the established generation and its
    // fenced snapshot while the subscription handle is still pending.
    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(2);
    expect(transport.requests).toHaveLength(2);

    const replacementValidated = nextSessionChange(session);
    replacementSnapshot.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await replacementValidated;

    const connected = waitForSession(session, () => session.getState().status === 'connected');
    transport.releasePendingSubscribe();
    await connected;

    // A later in-loop store replacement owns a distinct fenced query.
    const laterSnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(laterSnapshot.promise);
    replacement.onMessage(
      eventBatch([event(1, 'run_finished', {run_id: 'run-1'})], {
        store_id: 'later-store',
        through_sequence: 1,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'later-store',
    });
    const laterValidated = nextSessionChange(session);
    laterSnapshot.resolve(snapshotResponse({sequence: 1, status: 'completed'}));
    await laterValidated;
    replacement.onDisconnect(disconnect('later store complete'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);

    await session.close();
    replacement.onDisconnect(disconnect('closed session'));
    expect(scheduler.runNext()).toBe(false);
  });

  test('resumes when a lifecycle snapshot ends ahead of the event stream', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));

    await session.start();
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'running'});
    const initial = transport.subscriptions[0];
    if (initial === undefined) throw new Error('Initial subscription did not open');

    transport.snapshots.push(snapshotResponse({sequence: 3, status: 'completed'}));
    transport.silentDials = 1;
    const resumed = transport.nextSubscription();
    const observedTerminal = waitForSession(
      session,
      () => session.store.getState().status === 'completed',
    );
    lifecycle.emit('online');
    await observedTerminal;

    // The accepted snapshot proves that the current subscription has not delivered the tail.
    // Retire it during lifecycle recovery instead of treating terminal status as complete.
    expect(initial.closed).toBe(true);
    const subscription = await resumed;
    expect(subscription).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    const recovered = waitForSession(session, () => {
      return session.getState().status === 'connected' && session.store.getState().sequence === 3;
    });
    subscription.onMessage(outputBatch('store-1', 2, 'last output\n'));
    subscription.onMessage(outputBatch('store-1', 3, 'terminal output\n'));
    await recovered;
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'last output\n',
      'terminal output\n',
    ]);

    // Once the fold reaches the accepted snapshot, an ordinary terminal close stays quiet and
    // does not start a reconnect loop.
    subscription.onDisconnect(disconnect('gateway exited'));
    expect(session.getState()).toEqual(healthy());
    expect(scheduler.pendingDelays()).toEqual([]);
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
    subscription.onDisconnect(disconnect('closed session'));
    expect(scheduler.runNext()).toBe(false);
    expect(transport.subscriptions).toHaveLength(2);
  });

  test('terminal stream closes redial exactly while the snapshot watermark is ahead', async () => {
    const terminalStatuses = [
      'completed',
      'failed',
      'stopped',
    ] as const satisfies readonly RunStatus[];

    for (const seed of SEEDS.slice(0, 12)) {
      const random = new Seeded(seed);
      const status = random.pick(terminalStatuses);
      const snapshotSequence = random.int(6) + 2;
      const lifecycle = new FakeLifecycle();
      const scheduler = new FakeClock();
      const {session, transport} = sessionWith(lifecycle, {
        reconnectDelaysMs: [0],
        scheduleTimeout: scheduler.scheduleTimeout,
      });
      transport.snapshots.push(snapshotResponse({sequence: snapshotSequence, status}));

      await session.start();
      expect(session.store.getState()).toMatchObject({sequence: 1, status});
      const initial = transport.subscriptions[0];
      if (initial === undefined) throw new Error(`Seed ${seed}: initial subscription did not open`);
      const failure = disconnect(`seed ${seed}: stream closed`);

      transport.silentDials = 1;
      const retrying = transport.nextSubscription();
      initial.onDisconnect(failure);
      expect({
        seed,
        status: session.getState().status,
        pending: scheduler.pendingDelays(),
      }).toEqual({seed, status: 'stale', pending: [0]});

      expect(scheduler.runNext()).toBe(true);
      const subscription = await retrying;
      expect(subscription).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

      // A resumed delivery at the existing cursor is ignored, and each missing event is folded
      // once in sequence. The snapshot watermark never advances the event cursor itself.
      subscription.onMessage(outputBatch('store-1', 1, 'duplicate output\n'));
      const recovered = waitForSession(session, () => {
        return (
          session.getState().status === 'connected' &&
          session.store.getState().sequence === snapshotSequence
        );
      });
      const expected: string[] = [];
      for (let sequence = 2; sequence <= snapshotSequence; sequence += 1) {
        const content = `seed ${seed}, event ${sequence}\n`;
        expected.push(content);
        subscription.onMessage(outputBatch('store-1', sequence, content));
      }
      await recovered;
      expect({
        seed,
        transcript: session.store.getState().transcript.map(entry => entry.content),
      }).toEqual({seed, transcript: expected});

      subscription.onDisconnect(disconnect(`seed ${seed}: terminal close`));
      expect({seed, state: session.getState(), pending: scheduler.pendingDelays()}).toEqual({
        seed,
        state: healthy(),
        pending: [],
      });

      await session.close();
      subscription.onDisconnect(disconnect(`seed ${seed}: closed session`));
      expect({
        seed,
        timerRan: scheduler.runNext(),
        subscriptions: transport.subscriptions.length,
      }).toEqual({seed, timerRan: false, subscriptions: 2});
    }
  });

  test('lets an in-flight initial bootstrap satisfy a terminal refresh watermark', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'running'}));
    transport.deferNextSubscribe();

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    await waitForSession(session, () => session.store.getState().status === 'running');
    expect(session.store.getState()).toMatchObject({sequence: 1, status: 'running'});

    transport.snapshots.push(snapshotResponse({sequence: 3, status: 'completed'}));
    const observedTerminal = waitForSession(
      session,
      () => session.store.getState().status === 'completed',
    );
    lifecycle.emit('online');
    await observedTerminal;

    // The initial dial already owns a fresh bootstrap. The lifecycle retry coalesces with it
    // instead of retiring it or opening a parallel socket, and its remaining events can satisfy
    // the snapshot watermark before the handshake returns.
    expect(initial.closed).toBe(false);
    expect(transport.subscriptions).toHaveLength(1);
    initial.onMessage(outputBatch('store-1', 2, 'penultimate output\n'));
    initial.onMessage(outputBatch('store-1', 3, 'last output\n'));
    transport.releasePendingSubscribe();
    await starting;

    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState()).toMatchObject({sequence: 3, status: 'completed'});
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'penultimate output\n',
      'last output\n',
    ]);
    expect(transport.subscriptions).toHaveLength(1);
    await session.close();
  });

  test('wakes after an OS sleep with an exhausted schedule and catches up exactly', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [],
      scheduleTimeout: scheduler.scheduleTimeout,
    });

    await session.start();
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(1);
    expect(transport.subscriptions[0]).toMatchObject({afterSequence: 0});

    lifecycle.setVisibility('hidden');
    lifecycle.setOnline(false);
    expect(session.getState().status).toBe('stale');
    transport.dropControlChannel(disconnect('laptop slept'));
    transport.subscriptions[0]?.onDisconnect(disconnect('laptop slept'));
    expect(scheduler.pendingDelays()).toEqual([]);

    // Network returns while the page is still suspended. The lifecycle policy
    // waits for visibility rather than claiming the hidden page can make
    // progress, and the empty schedule represents timers exhausted during the
    // wall-clock gap.
    lifecycle.setOnline(true);
    expect(transport.subscriptions).toHaveLength(1);

    transport.silentDials = 1;
    const resumed = transport.nextSubscription();
    const recovered = waitForSession(session, () => {
      return (
        session.getState().status === 'connected' &&
        session.getState().controls.status === 'connected' &&
        session.store.getState().sequence === 2
      );
    });
    lifecycle.setVisibility('visible');
    expect(transport.reconnectCalls).toBe(1);
    const subscription = await resumed;
    expect(subscription).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'store-1'},
    });
    subscription.onMessage(outputBatch('store-1', 2, 'caught up after sleep\n'));
    await recovered;
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(2);
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'caught up after sleep\n',
    ]);

    await session.close();
    expect(transport.closeCalls).toBe(1);
    expect(lifecycle.listenerCount('online')).toBe(0);
    lifecycle.setOnline(false);
    expect(transport.subscriptions).toHaveLength(2);
  });

  test('wakes a throttled tab without waiting for its skipped reconnect timer', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [50],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();

    lifecycle.setVisibility('hidden');
    transport.dropControlChannel(disconnect('backgrounded'));
    transport.subscriptions[0]?.onDisconnect(disconnect('backgrounded'));
    expect(scheduler.pendingDelays()).toEqual([50]);

    transport.silentDials = 1;
    const resumed = transport.nextSubscription();
    const recovered = waitForSession(session, () => {
      return session.getState().status === 'connected' && session.store.getState().sequence === 2;
    });
    lifecycle.setVisibility('visible');
    expect(transport.reconnectCalls).toBe(1);
    const subscription = await resumed;

    expect(scheduler.pendingDelays()).toEqual([]);
    expect(subscription).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});
    subscription.onMessage(outputBatch('store-1', 2, 'caught up after throttling\n'));
    await recovered;

    // The wake retired the skipped reconnect timer: nothing is left to fire,
    // so no second subscription opens.
    expect(scheduler.runNext()).toBe(false);
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'caught up after throttling\n',
    ]);
    await session.close();
  });

  test('releases recovery ownership when control reconnect throws synchronously', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();

    const failure = new Error('control reconnect failed');
    transport.reconnectFailures.push(failure);
    lifecycle.emit('online');
    expect(session.getState()).toMatchObject({status: 'stale', error: failure});

    const recovered = transport.nextSubscription();
    session.reattach();
    expect(transport.reconnectCalls).toBe(2);
    await expect(recovered).resolves.toMatchObject({afterSequence: 0, options: undefined});
    await waitForSession(session, () => session.getState().status === 'connected');
    await session.close();
  });

  test('regression: a late start-up snapshot never overwrites a newer refresh snapshot', async () => {
    // The shell used to fold each loaded snapshot before the core checked its reply, so the
    // start-up snapshot, answered after an overlapping refresh's, rolled the run's status back.
    // Both are at one sequence, where the run fold itself cannot tell which is newer.
    const older = snapshotResponse({sequence: 1, status: 'running'});
    const newer = snapshotResponse({sequence: 1, status: 'paused'});
    for (const startupFirst of [false, true]) {
      const lifecycle = new FakeLifecycle();
      const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
      const startup = deferred<ProtocolResponse>();
      const refresh = deferred<ProtocolResponse>();
      transport.snapshots.push(startup.promise, refresh.promise);
      const started = session.start();
      lifecycle.emit('online');
      expect(transport.requests).toHaveLength(2);

      const status = () => session.store.getState().status;
      const answerStartup = async () => {
        startup.resolve(older);
        await started;
      };
      const answerRefresh = async () => {
        refresh.resolve(newer);
        await waitForSession(session, () => status() === 'paused');
      };
      // The refresh's stream retry is not part of this ordering assertion.
      // Keep its bootstrap silent so only the two fenced snapshot replies can
      // change the fold.
      transport.silentDials = 1;
      if (startupFirst) {
        await answerStartup();
        await answerRefresh();
      } else {
        await answerRefresh();
        await answerStartup();
      }
      expect({startupFirst, status: status()}).toEqual({
        startupFirst,
        status: 'paused',
      });
      await session.close();
    }
  });

  test('promotes an overlapping lifecycle retry to an explicit reattach', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(session.getState().status).toBe('stale');

    const snapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(snapshot.promise);
    lifecycle.emit('online');
    expect(transport.requests).toHaveLength(2);
    expect(transport.reconnectCalls).toBe(1);

    // The lifecycle retry owns the one control redial and snapshot request,
    // but an explicit user request that arrives before that snapshot settles
    // must promote its eventual stream recovery to a fresh bootstrap.
    session.reattach();
    session.reattach();
    lifecycle.emit('online');
    expect(transport.requests).toHaveLength(2);
    expect(transport.reconnectCalls).toBe(1);
    expect(transport.subscriptions).toHaveLength(1);

    const recovered = transport.nextSubscription();
    snapshot.resolve(snapshotResponse({sequence: 1}));
    expect(await recovered).toMatchObject({afterSequence: 0, options: undefined});
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
  });

  test('keeps an explicit reattach behind a lifecycle stream dial', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();

    transport.silentDials = 1;
    transport.deferNextSubscribe();
    const retrying = transport.nextSubscription();
    const retiring = transport.nextSubscriptionClose();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    lifecycle.emit('online');
    await retiring;

    // The retry has taken ownership and begun retiring the stale handle, but
    // the session's flight must already be reusable when its public caller can
    // observe that transition. Otherwise this stronger intent is dropped in a
    // Promise.finally tail after the retry has already been dispatched.
    transport.snapshots.push(new Error('reattach snapshot failed'));
    const refreshed = waitForSession(
      session,
      () => session.getState().error?.message === 'reattach snapshot failed',
    );
    session.reattach();
    session.reattach();
    expect(transport.requests).toHaveLength(2);

    const resumed = await retrying;
    expect(resumed).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    resumed.onMessage(outputBatch('store-1', 2, 'superseded retry\n'));
    expect(session.store.getState().sequence).toBe(1);
    expect(session.store.getState().transcript).toEqual([]);

    const fresh = transport.nextSubscription();
    transport.releasePendingSubscribe();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});
    await refreshed;

    expect(
      transport.subscriptions.slice(1).map(({afterSequence, options}) => ({
        afterSequence,
        options,
      })),
    ).toEqual([
      {afterSequence: 1, options: {storeId: 'store-1'}},
      {afterSequence: 0, options: undefined},
    ]);
    expect(resumed.closed).toBe(true);
    await session.close();
  });

  test('queues one reattach behind a recovery cursor dial before its first batch', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.silentDials = 2;
    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    initial.onMessage(streamBatch('store-1', 1));
    await starting;

    transport.deferNextSubscribe();
    const retrying = transport.nextSubscription();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(scheduler.runNext()).toBe(true);
    expect(await retrying).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'store-1'},
    });

    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(2);

    const fresh = transport.nextSubscription();
    transport.releasePendingSubscribe();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});

    expect(transport.subscriptions).toHaveLength(3);
    await session.close();
  });

  test('allows another Reattach after a fresh bootstrap fails before its first batch', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    await session.start();

    transport.silentDials = 2;
    const failedDial = transport.nextSubscription();
    session.reattach();
    const failed = await failedDial;
    failed.onDisconnect(disconnect('fresh bootstrap failed'));
    expect(session.getState()).toMatchObject({
      status: 'stale',
      error: {message: 'fresh bootstrap failed'},
    });

    const retriedDial = transport.nextSubscription();
    session.reattach();
    const retried = await retriedDial;
    expect(retried).toMatchObject({afterSequence: 0, options: undefined});
    expect(transport.subscriptions).toHaveLength(3);

    await session.close();
  });

  test('coalesces reattach with a pending initial stream bootstrap', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.deferNextSubscribe();
    const initialSubscription = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialSubscription;

    // start() has completed its fenced snapshot but still owns the first
    // sequence-zero handshake. Reattach is the same fresh intent, so it must not create a
    // second socket whose handle could later be orphaned by the first one.
    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(1);

    transport.releasePendingSubscribe();
    await starting;
    await session.close();
    expect(initial.closed).toBe(true);
    expect(transport.subscriptions.every(subscription => subscription.closed)).toBe(true);
  });

  test('retires recovery when its replacement generation completes startup', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.silentDials = 2;
    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    await settle();

    initial.onDisconnect(disconnect('lost before the first batch'));
    expect(session.getState().status).toBe('stale');

    const replacementDial = transport.nextSubscription();
    session.reattach();
    const replacement = await replacementDial;
    replacement.onMessage(streamBatch('replacement-store', 1));
    await starting;
    await settle();
    expect(session.getState().status).toBe('connected');

    // The replacement generation satisfied both startup and the overlapping
    // recovery. A later explicit recovery must not be suppressed by a stale
    // recovery owner left in the core.
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(3);
    await session.close();
  });

  test('retries a failed startup replacement whose subscription handle is pending', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.silentDials = 2;
    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    await settle();

    initial.onDisconnect(disconnect('lost before the first batch'));
    const failure = new Error('replacement snapshot failed');
    transport.snapshots.push(failure);
    transport.deferNextSubscribe();
    const replacementDial = transport.nextSubscription();
    session.reattach();
    const replacement = await replacementDial;

    // The replacement batch is observable before subscribe() returns its
    // handle. Its fenced snapshot fails while that fresh dial still owns the
    // stream.
    replacement.onMessage(streamBatch('replacement-store', 1));
    await waitForSession(session, () => session.getState().error === failure);
    await starting;

    // Reattach coalesces with that already-established generation, but still
    // owns a new fenced snapshot. Adopting the late handle cannot erase the
    // first failure while the replacement proof is pending.
    const retrySnapshot = deferred<ProtocolResponse>();
    transport.snapshots.push(retrySnapshot.promise);
    session.reattach();
    await settle();
    expect(transport.requests).toHaveLength(2);
    expect(transport.requests.at(-1)).toMatchObject({
      type: 'query.snapshot',
      expected_store_id: 'replacement-store',
    });
    transport.releasePendingSubscribe();
    await settle();
    expect(session.getState()).toMatchObject({status: 'stale', error: failure});

    const recovered = waitForSession(session, () => session.getState().status === 'connected');
    retrySnapshot.resolve(snapshotResponse({sequence: 1}));
    await recovered;

    // Both the startup and the retry owner have settled, so a later explicit
    // Reattach can establish another fresh generation.
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(3);
    await session.close();
  });

  test('accepts a successful recovery snapshot before its pending handle is adopted', async () => {
    const connection = new PendingHandleConnection();
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: () => connection,
    });
    const starting = session.start();
    await waitForSession(session, () => session.getState().error === connection.failure);
    session.reattach();
    await waitForSession(session, () => session.getState().hasSnapshot);
    expect(session.getState()).toMatchObject({status: 'stale', error: connection.failure});

    // The successful fenced retry is authoritative, but only adopting the same stream generation
    // supplies the connection proof needed to clear the earlier snapshot failure.
    connection.release();
    await starting;
    expect(session.getState()).toMatchObject({status: 'connected', error: null});
    await session.close();
  });

  test('does not let a snapshot success erase a newer stream fault', async () => {
    const connection = new FaultBeforeSnapshotConnection();
    const session = new WebSession({
      lifecycle: new FakeLifecycle(),
      connect: () => connection,
    });
    await session.start();
    expect(session.getState()).toMatchObject({status: 'stale', error: connection.firstFailure});

    session.reattach();
    await connection.rebootstrapStarted;
    connection.establishFaultedReplacement();
    expect(session.getState()).toMatchObject({status: 'stale', error: connection.streamFault});

    const applied = waitForSession(session, () => session.getState().hasSnapshot);
    connection.resolveReplacementSnapshot();
    await applied;

    expect(session.getState()).toMatchObject({status: 'stale', error: connection.streamFault});
    await session.close();
  });

  test('re-bootstraps when one store raises its declared history floor', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100, 10));
    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 20, 50));

    expect(session.store.getState().sequence).toBe(20);
    expect(session.store.getState().historyAfterSequence).toBe(50);
    await session.close();
  });

  test('uses the fresh-path empty-store rule instead of keeping a stale identity', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100));
    transport.subscriptions[0]?.onMessage(streamBatch('', 2));

    expect(session.store.getState().sequence).toBe(2);
    expect(transport.requests.at(-1)).toEqual({
      type: 'query.snapshot',
      expected_store_id: '',
    });
    await session.close();
  });

  test('replaces the fold when a store swap reboots mid-subscription', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    transport.subscriptions[0]?.onMessage(outputBatch('old-store', 50, 'retired output\n'));
    expect(session.store.getState().sequence).toBe(50);
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'retired output\n',
    ]);

    transport.subscriptions[0]?.onMessage(
      eventBatch([event(2, 'agent_output_chunk', 'replacement output\n')], {
        store_id: 'new-store',
        through_sequence: 2,
        history_after_sequence: 0,
        rebootstrap: true,
      }),
    );

    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState()).toMatchObject({
      sequence: 2,
      historyAfterSequence: 0,
    });
    expect(session.store.getState().transcript.map(entry => entry.content)).toEqual([
      'replacement output\n',
    ]);
    await session.close();
  });

  test('rejects an invalid declared floor without mutating the fold', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();
    const before = session.store.getState();

    expect(() => transport.subscriptions[0]?.onMessage(streamBatch('store-1', 2, -1))).toThrow(
      'event_batch.history_after_sequence',
    );

    expect(session.store.getState()).toBe(before);
    await session.close();
  });

  test('keeps the reached history floor across a resumed batch', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();
    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100, 50));
    transport.silentDials = 1;
    const resumed = transport.nextSubscription();

    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(scheduler.runNext()).toBe(true);
    const subscription = await resumed;
    subscription.onMessage(streamBatch('run-store', 101, 0));

    expect(subscription).toMatchObject({
      afterSequence: 100,
      options: {storeId: 'run-store'},
    });
    expect(session.store.getState().sequence).toBe(101);
    expect(session.store.getState().historyAfterSequence).toBe(50);
    await session.close();
  });

  test('keeps a failed snapshot stale until an explicit wake succeeds', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(new Error('snapshot unavailable'));

    await session.start();
    expect(session.getState().status).toBe('stale');
    expect(session.getState().hasSnapshot).toBe(false);
    expect(transport.subscriptions).toHaveLength(1);

    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.getState()).toEqual(healthy());
    expect(transport.requests).toHaveLength(2);

    await session.close();
  });

  test('reports a dead control channel as its own state, not as a stale stream', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();
    expect(session.getState()).toEqual(healthy());
    const published: string[] = [];
    session.subscribe(() => published.push(session.getState().controls.status));

    transport.dropControlChannel(disconnect('gateway restarted'));

    const dead = session.getState();
    // The transcript is still streaming; only the command path is down, and the
    // two are reported as the separate facts they are.
    expect(dead.status).toBe('connected');
    expect(dead.error).toBeNull();
    expect(dead.controls).toEqual(lost('gateway restarted'));
    expect(published).toEqual(['disconnected']);

    transport.recoverControlChannel();
    expect(session.getState()).toEqual(healthy());
    expect(published).toEqual(['disconnected', 'connected']);

    await session.close();
  });

  test('redials the control channel without touching the event stream', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    expect(session.getState().controls.status).toBe('disconnected');
    const requests = transport.requests.length;

    session.reconnectControls();

    // The affordance reaches the transport's redial verb and the recovery it
    // produces reaches the session's state. Issuing a request instead would
    // only queue behind the backoff the outage already armed, so a click could
    // change nothing observable for the length of the schedule.
    expect(transport.reconnectCalls).toBe(1);
    expect(session.getState().controls).toEqual({status: 'connected'});
    // And it is the command path only: a live transcript is not resubscribed.
    await settle();
    expect(transport.subscriptions).toHaveLength(1);
    expect(transport.requests).toHaveLength(requests);

    await session.close();
    transport.dropControlChannel(disconnect('gateway restarted'));
    session.reconnectControls();
    expect(transport.reconnectCalls).toBe(1);
  });

  test('publishes a retry in progress so the affordance can say it is working', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    expect(session.getState().controls).toEqual(lost('gateway restarted'));

    // A dial starting is a change a frontend renders (the button goes dead
    // while `reconnect()` would no-op), so it must not be swallowed by a dedup
    // that only compares the status.
    transport.retryControlChannel(disconnect('gateway restarted'));
    expect(session.getState().controls).toEqual(lost('gateway restarted', true));

    await session.close();
  });

  test('publishes an outage on an ended run rather than judging it', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'completed'}));

    await session.start();
    expect(session.store.getState().status).toBe('completed');

    // The channel is genuinely undeliverable, so the session says so. Whether
    // that is worth an affordance on a finished run is a presentation judgment
    // and belongs to `connectionBanners`, which withholds the banner here; see
    // `banners.test.ts`. Deciding it at report time instead latches the banner
    // on, because a drop reported while the run's terminal event is still in
    // flight never gets re-examined when that event lands.
    transport.dropControlChannel(disconnect('gateway exited'));
    expect(session.getState().controls).toEqual(lost('gateway exited'));

    await session.close();
  });

  /**
   * The #1044 regression, at the seam where the symptom is visible: a run
   * reopened after it finished, whose stream faults before its bootstrap batch.
   *
   * `start()` awaits the snapshot before it subscribes, so the terminal status
   * is always in the store by the time the socket can fault, and the fold is
   * empty because a snapshot carries status and no events. Suppressing the
   * report left the page with nothing but a `completed` chip over an empty
   * transcript.
   */
  test('says the transcript stopped short when the stream faults on an ended run', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({sequence: 0, status: 'completed'}));
    transport.silentDials = 1;

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    initial.onMessage(
      eventBatch([], {store_id: 'store-1', through_sequence: 0, history_after_sequence: 0}),
    );
    await starting;
    expect(session.store.getState().status).toBe('completed');
    // Terminal status, nothing folded: a snapshot carries the status and no
    // events, so `reduceSnapshot` never advances the cursor or the transcript.
    expect(session.store.getState().sequence).toBe(0);
    expect(session.store.getState().transcript).toEqual([]);
    expect(session.getState()).toEqual(healthy());

    // The error `WebSocketTransport` injects when `parseServerMessage` rejects
    // a live frame, delivered where it delivers it: `onDisconnect` on an
    // already-subscribed socket.
    const parseFailure = new BackendClientError('parse', 'Invalid event batch message');
    transport.subscriptions[0]?.onDisconnect(parseFailure);

    expect(session.getState()).toEqual({
      hasSnapshot: true,
      status: 'stale',
      error: parseFailure,
      controls: {status: 'connected'},
    });
    // The page says the transcript is short and offers the explicit recovery
    // that can reload an ended run. The command path itself is still fine.
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.ended, reattach: true},
      controls: null,
    });

    // Automatic redial policy for an ended run is unchanged. The user action
    // is different: establish a fresh bootstrap, then load its fenced snapshot
    // instead of asking the terminal-status predicate for permission to resume.
    expect(transport.subscriptions).toHaveLength(1);
    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'completed'}));
    const recovered = waitForSession(session, () => {
      return (
        session.getState().status === 'connected' &&
        session.store.getState().sequence === 1 &&
        transport.subscriptions.length > 1
      );
    });
    session.reattach();
    await recovered;
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState().status).toBe('completed');

    await session.close();
  });

  test('surfaces a stream protocol error and re-bootstraps an ended run on request', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse({sequence: 0, status: 'completed'}));
    transport.silentDials = 1;

    const initialDial = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialDial;
    initial.onMessage(
      eventBatch([], {store_id: 'store-1', through_sequence: 0, history_after_sequence: 0}),
    );
    await starting;
    transport.subscriptions[0]?.onMessage({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'Replay failed',
      diagnostic: {
        id: 'stream-1',
        code: 'stream_failed',
        summary: 'The replay stream failed.',
        scope: 'protocol',
      },
    });

    expect(session.getState().error).toBeInstanceOf(ServerError);
    expect(session.getState()).toMatchObject({
      status: 'stale',
      error: {
        message: 'Replay failed',
        diagnostic: {id: 'stream-1', code: 'stream_failed', scope: 'protocol'},
      },
    });
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.ended, reattach: true},
      controls: null,
    });

    transport.snapshots.push(snapshotResponse({sequence: 1, status: 'completed'}));
    const recovered = waitForSession(session, () => {
      return (
        session.getState().status === 'connected' &&
        session.store.getState().sequence === 1 &&
        transport.subscriptions.length > 1
      );
    });
    session.reattach();
    await recovered;
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.store.getState().status).toBe('completed');

    await session.close();
  });

  test('offers a reattach for the same fault while the run can still stream', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    expect(session.store.getState().sequence).toBe(1);

    transport.subscriptions[0]?.onDisconnect(
      new BackendClientError('parse', 'Invalid event batch message'),
    );

    // Read before the redial settles, which is where the banner is on screen:
    // the drop is published in the same task as the fault. Same fault as above
    // and a different statement, because this gap can still close, and the
    // affordance that asks for it sooner rides inside the banner.
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.live, reattach: true},
      controls: null,
    });

    await settle();
    // The stream redialed on its own schedule and the gap closed: the
    // re-bootstrap folded the batch the faulted dial never delivered.
    expect(transport.subscriptions.length).toBeGreaterThan(1);
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(2);

    await session.close();
  });

  test('keeps reconnect and reattach available for a forward-compatible run status', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(
      snapshotResponse({sequence: 1, status: 'waiting_for_capacity' as RunStatus}),
    );

    await session.start();
    const resumed = transport.nextSubscription();
    const parseFailure = new BackendClientError('parse', 'Invalid event batch message');
    transport.subscriptions[0]?.onDisconnect(parseFailure);

    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      page: null,
      stream: {message: STREAM_BANNER_COPY.live, reattach: true},
      controls: null,
    });
    expect(scheduler.pendingDelays()).toEqual([0]);

    expect(scheduler.runNext()).toBe(true);
    await resumed;
    expect(transport.subscriptions).toHaveLength(2);

    await session.close();
  });

  test('clears a controls banner while a lifecycle refresh finds the run ended', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    // Newer than the batch `start()` already folded, so the fold takes it.
    transport.snapshots.push(snapshotResponse({status: 'completed', sequence: 5}));
    const refreshed = waitForSession(session, () => {
      return (
        session.store.getState().status === 'completed' &&
        session.getState().controls.status === 'connected'
      );
    });
    session.reattach();
    await refreshed;
    expect(session.store.getState().status).toBe('completed');
    // The same wake that refreshes the snapshot redials controls first, so the
    // banner raised while the run was live is already down when the terminal
    // snapshot lands.
    expect(session.getState().controls).toEqual({status: 'connected'});

    await session.close();
  });

  test('ignores a control-channel report after the session is closed', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    await session.close();
    transport.dropControlChannel(disconnect('too late'));

    expect(session.getState()).toEqual(healthy());
  });
});

/** A typed transport failure, which is all the channel ever reports. */
function disconnect(message: string): BackendClientError {
  return new BackendClientError('disconnected', message);
}

/** The typed server refusal for an expected-store snapshot fence. */
function storeReplacedResponse(): ProtocolResponse {
  return {
    ok: false,
    request_id: 'request-store-replaced',
    error: 'Request failed',
    diagnostic: {
      code: 'snapshot_store_replaced',
      summary: 'Request failed',
      scope: 'request',
    },
  };
}

/** The state a channel reports after losing a connection it had. */
function lost(message: string, retrying = false): ControlChannelState {
  return {status: 'disconnected', error: disconnect(message), everConnected: true, retrying};
}

/** The state of a session whose stream and control channel are both healthy. */
function healthy(): ReturnType<WebSession['getState']> {
  return {hasSnapshot: true, status: 'connected', error: null, controls: {status: 'connected'}};
}

/**
 * A session over an in-memory transport, built the way the session builds the
 * real one: the factory receives the session's observers, so the control
 * channel has somewhere to report.
 */
function sessionWith(
  lifecycle: FakeLifecycle,
  options: TransportTiming = {},
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

function streamBatch(storeId: string, sequence: number, historyAfterSequence = 0): ServerMessage {
  return eventBatch([event(sequence, 'server_ready')], {
    through_sequence: sequence,
    store_id: storeId,
    history_after_sequence: historyAfterSequence,
  });
}

function outputBatch(storeId: string, sequence: number, content: string): ServerMessage {
  return eventBatch([event(sequence, 'agent_output_chunk', content)], {
    through_sequence: sequence,
    store_id: storeId,
    history_after_sequence: 0,
  });
}

function waitForSession(session: WebSession, predicate: () => boolean): Promise<void> {
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

/** Resolves after the session core next publishes a distinct state. */
function nextSessionChange(session: WebSession): Promise<void> {
  return new Promise(resolve => {
    const unsubscribe = session.subscribe(() => {
      unsubscribe();
      resolve();
    });
  });
}

async function settle(): Promise<void> {
  await new Promise<void>(resolve => setTimeout(resolve, 0));
  await Promise.resolve();
}

function deferred<T>(): {readonly promise: Promise<T>; readonly resolve: (value: T) => void} {
  let complete = (_value: T): void => {
    throw new Error('Deferred promise was not initialized');
  };
  const promise = new Promise<T>(resolve => {
    complete = resolve;
  });
  return {promise, resolve: complete};
}
