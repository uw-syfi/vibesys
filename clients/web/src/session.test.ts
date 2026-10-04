import {describe, expect, test} from 'bun:test';
import {
  BackendClientError,
  type ControlChannelState,
  type ControlTransport,
  type ProtocolResponse,
  type RequestInput,
  type ScheduleTimeout,
  type ServerMessage,
  type SubscribeOptions,
  sameControlChannelState,
} from '@vibesys/backend-client';
import {event, eventBatch, snapshotResponse} from '@vibesys/backend-client/testing';
import {connectionBanners, STREAM_BANNER_COPY} from './banners.js';
import {
  type BrowserLifecycle,
  WebSession,
  type WebSessionOptions,
  type WebSessionTransportHooks,
  webSocketUrlFromLocation,
} from './session.js';

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

/** Deterministic scheduler for the stream's reconnect backoff. */
class ManualScheduler {
  readonly #pending: Array<{callback: () => void; cancelled: boolean}> = [];

  readonly scheduleTimeout: ScheduleTimeout = callback => {
    const pending = {callback, cancelled: false};
    this.#pending.push(pending);
    return () => {
      pending.cancelled = true;
    };
  };

  runNext(): void {
    const pending = this.#pending.shift();
    if (pending === undefined) throw new Error('No reconnect is scheduled');
    if (!pending.cancelled) pending.callback();
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
  readonly snapshots: Array<ProtocolResponse | Error> = [];
  readonly #subscriptionWaiters: Array<(subscription: SubscriptionRecord) => void> = [];
  /**
   * How many upcoming dials connect without delivering their bootstrap batch,
   * as a socket that dies between `subscribed` and the first batch does: the
   * gateway sends `SubscribedMessage` before `_write_bootstrap`, so a
   * subscription can be live with nothing folded under it.
   */
  silentDials = 0;
  closeCalls = 0;
  reconnectCalls = 0;
  readonly #hooks: WebSessionTransportHooks;
  #controls: ControlChannelState = {status: 'connected'};

  constructor(hooks: WebSessionTransportHooks) {
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
    return response ?? snapshotResponse();
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
    return {
      close: async () => {
        record.closed = true;
      },
    };
  }

  async close(): Promise<void> {
    this.closeCalls++;
  }

  /** Resolves when the stream makes its next dial. */
  nextSubscription(): Promise<SubscriptionRecord> {
    return new Promise(resolve => this.#subscriptionWaiters.push(resolve));
  }
}

describe('WebSession', () => {
  test('maps the page URL to the browser WebSocket endpoint', () => {
    expect(
      webSocketUrlFromLocation(
        new URL(
          'http://localhost:4173/runs/demo?token=secret&unused=ignored',
        ) as unknown as Location,
      ),
    ).toBe('ws://localhost:4173/ws');
    expect(
      webSocketUrlFromLocation(
        new URL('https://example.test/app?token=encoded%20token') as unknown as Location,
      ),
    ).toBe('wss://example.test/ws');
  });

  test('maps a browser harness capability URL to the gateway WebSocket endpoint', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dsecret',
      } as Location),
    ).toBe('ws://127.0.0.1:8765/ws');
  });

  test('never forwards the page capability token to a foreign gateway authority', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'http://127.0.0.1:8765/?token=secret&gateway=http%3A%2F%2F127.0.0.1%3A5173%2F',
      } as Location),
    ).toBe('ws://127.0.0.1:5173/ws');
  });

  test('never puts a capability token in a WebSocket URL', () => {
    const pageOrigins = ['http://127.0.0.1:8765', 'https://gateway.test'];
    const gatewayValues = [
      null,
      '/',
      'http://127.0.0.1:5173/',
      'http://127.0.0.1:5173/?token=gateway-token',
      'https://elsewhere.test/',
      '//elsewhere.test/',
      'http://127.0.0.1:8765@elsewhere.test/',
    ];
    const cases = pageOrigins.flatMap(origin =>
      gatewayValues.flatMap(gateway =>
        [null, 'page-token'].map(pageToken => ({origin, gateway, pageToken})),
      ),
    );

    const results = cases.map(({origin, gateway, pageToken}) => {
      const page = new URL(origin);
      if (pageToken !== null) page.searchParams.set('token', pageToken);
      if (gateway !== null) page.searchParams.set('gateway', gateway);
      const socket = new URL(webSocketUrlFromLocation({href: page.href} as Location));
      return {
        page: page.href,
        socket: socket.href,
        sent: socket.searchParams.get('token'),
        pageAuthority: socket.origin === origin.replace(/^http/, 'ws'),
      };
    });

    expect(results.filter(result => result.sent !== null)).toEqual([]);
  });

  test('maps a direct gateway capability URL to a secure WebSocket endpoint', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'https://127.0.0.1:8765/?token=secret',
      } as Location),
    ).toBe('wss://127.0.0.1:8765/ws');
  });

  test('wakes a stale session after the browser returns online', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(1);
    expect(transport.subscriptions[0]).toMatchObject({afterSequence: 0});

    lifecycle.setOnline(false);
    expect(session.getState().status).toBe('stale');

    lifecycle.setVisibility('hidden');
    lifecycle.setOnline(true);
    await settle();
    expect(transport.subscriptions).toHaveLength(1);

    lifecycle.setVisibility('visible');
    await settle();
    expect(transport.subscriptions).toHaveLength(2);
    expect(transport.subscriptions[1]).toMatchObject({
      afterSequence: 1,
      options: {storeId: 'store-1'},
    });
    expect(session.getState()).toEqual(healthy());
    expect(session.store.getState().sequence).toBe(2);

    await session.close();
    expect(transport.closeCalls).toBe(1);
    expect(lifecycle.listenerCount('online')).toBe(0);
    lifecycle.setOnline(false);
    expect(transport.subscriptions).toHaveLength(2);
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
    const scheduler = new ManualScheduler();
    const {session, transport} = sessionWith(lifecycle, {
      reconnectDelaysMs: [0],
      scheduleTimeout: scheduler.scheduleTimeout,
    });
    await session.start();
    transport.subscriptions[0]?.onMessage(streamBatch('run-store', 100, 50));
    transport.silentDials = 1;
    const resumed = transport.nextSubscription();

    transport.subscriptions[0]?.onDisconnect(disconnect('gateway restarted'));
    scheduler.runNext();
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
    await settle();

    expect(session.getState()).toEqual({
      status: 'stale',
      error: parseFailure,
      controls: {status: 'connected'},
    });
    // The page says the transcript is short and does not promise it will fill
    // in, and it offers neither affordance: the run cannot be resubscribed and
    // the command path is fine.
    expect(connectionBanners(session.store.getState(), session.getState())).toEqual({
      stream: {message: STREAM_BANNER_COPY.ended, reattach: false},
      controls: null,
    });

    // The redial policy for an ended run is unchanged: nothing was dialed
    // again, and the withheld `Reattach` would have been a no-op anyway.
    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(1);

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

  test('clears a controls banner raised before the run ended', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(disconnect('gateway restarted'));
    // Newer than the batch `start()` already folded, so the fold takes it.
    transport.snapshots.push(snapshotResponse({status: 'completed', sequence: 5}));
    session.reattach();
    await settle();
    expect(session.store.getState().status).toBe('completed');
    expect(session.getState().controls.status).toBe('disconnected');

    // A recovery is reported whatever the run's status, so a banner raised
    // while the run was live comes down on its own.
    transport.recoverControlChannel();
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
  return {status: 'connected', error: null, controls: {status: 'connected'}};
}

/**
 * A session over an in-memory transport, built the way the session builds the
 * real one: the factory receives the session's observers, so the control
 * channel has somewhere to report.
 */
function sessionWith(
  lifecycle: FakeLifecycle,
  options: Pick<WebSessionOptions, 'reconnectDelaysMs' | 'scheduleTimeout'> = {},
): {
  readonly session: WebSession;
  readonly transport: FakeTransport;
} {
  const built: FakeTransport[] = [];
  const session = new WebSession({
    lifecycle,
    transport: hooks => {
      const transport = new FakeTransport(hooks);
      built.push(transport);
      return transport;
    },
    reconnectDelaysMs: options.reconnectDelaysMs ?? [0],
    ...(options.scheduleTimeout === undefined ? {} : {scheduleTimeout: options.scheduleTimeout}),
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

async function settle(): Promise<void> {
  await new Promise<void>(resolve => setTimeout(resolve, 0));
  await Promise.resolve();
}
