import {describe, expect, test} from 'bun:test';
import {
  BackendClientError,
  type ControlChannelState,
  type ControlTransport,
  type ProtocolResponse,
  type RequestInput,
  type RunStatus,
  ServerError,
  type ServerMessage,
  type SubscribeOptions,
  sameControlChannelState,
} from '@vibesys/backend-client';
import {event, eventBatch, FakeClock, snapshotResponse} from '@vibesys/backend-client/testing';
import {connectRun, type TransportHooks, type TransportTiming} from '../../../platform/backend.js';
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

describe('WebSession', () => {
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
    await session.close();
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
    expect(transport.requests).toHaveLength(3);
    await refreshed;

    const resumed = await retrying;
    expect(resumed).toMatchObject({afterSequence: 1, options: {storeId: 'store-1'}});

    resumed.onMessage(outputBatch('store-1', 2, 'superseded retry\n'));
    expect(session.store.getState().sequence).toBe(1);
    expect(session.store.getState().transcript).toEqual([]);

    const fresh = transport.nextSubscription();
    transport.releasePendingSubscribe();
    await expect(fresh).resolves.toMatchObject({afterSequence: 0, options: undefined});

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

  test('coalesces reattach with a recovery bootstrap before the first batch', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.silentDials = 2;
    await session.start();

    transport.deferNextSubscribe();
    const retrying = transport.nextSubscription();
    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    expect(scheduler.runNext()).toBe(true);
    expect(await retrying).toMatchObject({afterSequence: 0, options: undefined});

    session.reattach();
    session.reattach();
    expect(transport.subscriptions).toHaveLength(2);

    const recovered = waitForSession(session, () => session.getState().status === 'connected');
    transport.releasePendingSubscribe();
    await recovered;

    expect(transport.subscriptions).toHaveLength(2);
    await session.close();
  });

  test('coalesces reattach with a pending initial stream bootstrap', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle, {reconnectDelaysMs: []});
    transport.deferNextSubscribe();
    const initialSubscription = transport.nextSubscription();
    const starting = session.start();
    const initial = await initialSubscription;

    // start() has completed its snapshot but still owns the first sequence-zero
    // handshake. Reattach is the same fresh intent, so it must not create a
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
    transport.snapshots.push(snapshotResponse({status: 'completed'}));

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
    transport.snapshots.push(snapshotResponse({status: 'completed'}));
    transport.silentDials = 1;

    await session.start();
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
    // is different: refresh the snapshot, then force a fresh bootstrap instead
    // of asking the terminal-status predicate for permission to resume.
    expect(transport.subscriptions).toHaveLength(1);
    transport.snapshots.push(snapshotResponse({status: 'completed'}));
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
    transport.snapshots.push(snapshotResponse({status: 'completed'}));
    transport.silentDials = 1;

    await session.start();
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

    transport.snapshots.push(snapshotResponse({status: 'completed'}));
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
    transport.silentDials = 1;

    await session.start();
    expect(session.store.getState().sequence).toBe(0);

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
    expect(session.store.getState().sequence).toBe(1);

    await session.close();
  });

  test('keeps reconnect and reattach available for a forward-compatible run status', async () => {
    const lifecycle = new FakeLifecycle();
    const scheduler = new FakeClock();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    transport.snapshots.push(snapshotResponse({status: 'waiting_for_capacity' as RunStatus}));
    transport.silentDials = 1;

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
