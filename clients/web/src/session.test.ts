import {describe, expect, test} from 'bun:test';
import type {
  ControlChannelState,
  ControlTransport,
  ProtocolResponse,
  RequestInput,
  RunEvent,
  ServerMessage,
  SubscribeOptions,
} from '@vibesys/backend-client';
import {
  type BrowserLifecycle,
  WebSession,
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
  closeCalls = 0;
  reconnectCalls = 0;
  readonly #hooks: WebSessionTransportHooks;
  #controls: ControlChannelState = {status: 'connected'};

  constructor(hooks: WebSessionTransportHooks) {
    this.#hooks = hooks;
  }

  /** The control channel lost its connection and reported the outage. */
  dropControlChannel(error: Error): void {
    this.#controls = {status: 'disconnected', error};
    this.#hooks.onConnectionState(this.#controls);
  }

  /** A redial brought the control channel back. */
  recoverControlChannel(): void {
    this.#controls = {status: 'connected'};
    this.#hooks.onConnectionState(this.#controls);
  }

  /**
   * Counted rather than acted on: the real one cancels an armed redial and
   * dials, and `websocket.test.ts` owns that. What this file checks is that the
   * session's affordance reaches the verb at all, and reaches nothing else.
   */
  reconnect(): void {
    this.reconnectCalls += 1;
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
    onMessage(eventBatch(`store-${this.subscriptions.length}`, afterSequence + 1));
    return {
      close: async () => {
        record.closed = true;
      },
    };
  }

  async close(): Promise<void> {
    this.closeCalls++;
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
    ).toBe('ws://localhost:4173/ws?token=secret');
    expect(
      webSocketUrlFromLocation(
        new URL('https://example.test/app?token=encoded%20token') as unknown as Location,
      ),
    ).toBe('wss://example.test/ws?token=encoded+token');
  });

  test('maps a browser harness capability URL to the gateway WebSocket endpoint', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dsecret',
      } as Location),
    ).toBe('ws://127.0.0.1:8765/ws?token=secret');
  });

  test('never forwards the page capability token to a foreign gateway authority', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'http://127.0.0.1:8765/?token=secret&gateway=http%3A%2F%2F127.0.0.1%3A5173%2F',
      } as Location),
    ).toBe('ws://127.0.0.1:5173/ws?token=');
  });

  test('sends a capability token only to the authority whose own URL carried it', () => {
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

    expect(results.filter(result => result.sent === 'page-token' && !result.pageAuthority)).toEqual(
      [],
    );
    expect(results.filter(result => result.sent !== '').length).toBeGreaterThan(0);
  });

  test('maps a direct gateway capability URL to a secure WebSocket endpoint', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'https://127.0.0.1:8765/?token=secret',
      } as Location),
    ).toBe('wss://127.0.0.1:8765/ws?token=secret');
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

    transport.dropControlChannel(new Error('gateway restarted'));

    const dead = session.getState();
    // The transcript is still streaming; only the command path is down, and the
    // two are reported as the separate facts they are.
    expect(dead.status).toBe('connected');
    expect(dead.error).toBeNull();
    expect(dead.controls).toEqual({
      status: 'disconnected',
      error: new Error('gateway restarted'),
    });
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
    transport.dropControlChannel(new Error('gateway restarted'));
    expect(session.getState().controls.status).toBe('disconnected');
    const requests = transport.requests.length;

    session.reconnectControls();

    // The affordance reaches the transport's redial verb. Issuing a request
    // instead would only queue behind the backoff the outage already armed, so
    // a click could change nothing observable for the length of the schedule.
    expect(transport.reconnectCalls).toBe(1);
    // And it is the command path only: a live transcript is not resubscribed.
    await settle();
    expect(transport.subscriptions).toHaveLength(1);
    expect(transport.requests).toHaveLength(requests);

    await session.close();
    session.reconnectControls();
    expect(transport.reconnectCalls).toBe(1);
  });

  test('does not raise the controls state for a run that has already ended', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    transport.snapshots.push(snapshotResponse('completed'));

    await session.start();
    expect(session.store.getState().status).toBe('completed');

    // Nothing is left to deliver and the gateway going away is how a finished
    // run ends, so an outage then is not news and must not put an affordance on
    // screen for a problem the user does not have.
    transport.dropControlChannel(new Error('gateway exited'));
    expect(session.getState().controls).toEqual({status: 'connected'});

    await session.close();
  });

  test('clears a controls banner raised before the run ended', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    transport.dropControlChannel(new Error('gateway restarted'));
    // Newer than the batch `start()` already folded, so the fold takes it.
    transport.snapshots.push(snapshotResponse('completed', 5));
    session.reattach();
    await settle();
    expect(session.store.getState().status).toBe('completed');
    expect(session.getState().controls.status).toBe('disconnected');

    // A recovery is reported whatever the run's status: the suppression above is
    // about not raising a banner, not about refusing to take one down.
    transport.recoverControlChannel();
    expect(session.getState().controls).toEqual({status: 'connected'});

    await session.close();
  });

  test('ignores a control-channel report after the session is closed', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);

    await session.start();
    await session.close();
    transport.dropControlChannel(new Error('too late'));

    expect(session.getState()).toEqual(healthy());
  });
});

/** The state of a session whose stream and control channel are both healthy. */
function healthy(): ReturnType<WebSession['getState']> {
  return {status: 'connected', error: null, controls: {status: 'connected'}};
}

/**
 * A session over an in-memory transport, built the way the session builds the
 * real one: the factory receives the session's observers, so the control
 * channel has somewhere to report.
 */
function sessionWith(lifecycle: FakeLifecycle): {
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
    reconnectDelaysMs: [0],
  });
  const transport = built[0];
  if (transport === undefined) throw new Error('WebSession did not build its transport');
  return {session, transport};
}

function snapshotResponse(status = 'running', sequence = 0): ProtocolResponse {
  return {
    ok: true,
    request_id: 'request-1',
    snapshot: {
      run_id: 'run-1',
      sequence,
      status,
    },
  } as ProtocolResponse;
}

function eventBatch(storeId: string, sequence: number): ServerMessage {
  const event: RunEvent = {
    sequence,
    timestamp: `2026-09-27T00:00:0${sequence}Z`,
    type: 'server_ready',
  };
  return {
    type: 'event_batch',
    events: [event],
    through_sequence: sequence,
    store_id: storeId,
    history_after_sequence: 0,
  } as ServerMessage;
}

async function settle(): Promise<void> {
  await new Promise<void>(resolve => setTimeout(resolve, 0));
  await Promise.resolve();
}
