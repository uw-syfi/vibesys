import {describe, expect, test} from 'bun:test';
import type {
  ProtocolResponse,
  RequestInput,
  RunEvent,
  ServerMessage,
  ServerTransport,
  SubscribeOptions,
} from '@vibesys/backend-client';
import {type BrowserLifecycle, WebSession, webSocketUrlFromLocation} from './session.js';

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

class FakeTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  readonly subscriptions: SubscriptionRecord[] = [];
  readonly snapshots: Array<ProtocolResponse | Error> = [];
  closeCalls = 0;

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

  test('maps a direct gateway capability URL to a secure WebSocket endpoint', () => {
    expect(
      webSocketUrlFromLocation({
        href: 'https://127.0.0.1:8765/?token=secret',
      } as Location),
    ).toBe('wss://127.0.0.1:8765/ws?token=secret');
  });

  test('wakes a stale session after the browser returns online', async () => {
    const lifecycle = new FakeLifecycle();
    const transport = new FakeTransport();
    const session = new WebSession({
      lifecycle,
      transport,
      reconnectDelaysMs: [0],
    });

    await session.start();
    expect(session.getState()).toEqual({status: 'connected', error: null});
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
    expect(session.getState()).toEqual({status: 'connected', error: null});
    expect(session.store.getState().sequence).toBe(2);

    await session.close();
    expect(transport.closeCalls).toBe(1);
    expect(lifecycle.listenerCount('online')).toBe(0);
    lifecycle.setOnline(false);
    expect(transport.subscriptions).toHaveLength(2);
  });

  test('keeps a failed snapshot stale until an explicit wake succeeds', async () => {
    const lifecycle = new FakeLifecycle();
    const transport = new FakeTransport();
    transport.snapshots.push(new Error('snapshot unavailable'));
    const session = new WebSession({
      lifecycle,
      transport,
      reconnectDelaysMs: [0],
    });

    await session.start();
    expect(session.getState().status).toBe('stale');
    expect(transport.subscriptions).toHaveLength(1);

    session.reattach();
    await settle();
    expect(transport.subscriptions).toHaveLength(2);
    expect(session.getState()).toEqual({status: 'connected', error: null});
    expect(transport.requests).toHaveLength(2);

    await session.close();
  });
});

function snapshotResponse(): ProtocolResponse {
  return {
    ok: true,
    request_id: 'request-1',
    snapshot: {
      run_id: 'run-1',
      sequence: 0,
      status: 'running',
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
