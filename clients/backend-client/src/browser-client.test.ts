import {afterEach, beforeEach, describe, expect, it, mock, spyOn} from 'bun:test';
import {
  BrowserBackendClient,
  PersistentEventStream,
  type ProtocolRequest,
  ServerError,
  type ServerMessage,
} from './browser.js';

class TestWebSocket extends EventTarget {
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: ProtocolRequest[] = [];
  closed = false;
  readonly url: string;
  constructor(url: string | URL) {
    super();
    this.url = String(url);
    sockets.push(this);
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data) as ProtocolRequest);
  }
  receive(message: unknown): void {
    this.onmessage?.(new MessageEvent('message', {data: JSON.stringify(message)}));
  }
  accept(): void {
    this.onopen?.();
    this.receive({
      type: 'subscribed',
      request_id: this.sent[0]?.request_id,
      run_id: 'run-1',
      latest_sequence: 10,
    });
  }
  close(): void {
    if (this.closed) return;
    this.closed = true;
    queueMicrotask(() => {
      this.dispatchEvent(new Event('close'));
      this.onclose?.();
    });
  }
}
let client: BrowserBackendClient;
let sockets: TestWebSocket[];
const noop = () => {};
const originalWebSocket = globalThis.WebSocket;
const fetchTarget: {fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>} =
  globalThis;
beforeEach(() => {
  client = new BrowserBackendClient('http://localhost:8765');
  sockets = [];
  globalThis.WebSocket = TestWebSocket as unknown as typeof WebSocket;
});
afterEach(async () => {
  await client.close();
  globalThis.WebSocket = originalWebSocket;
  mock.restore();
});
function response(request: ProtocolRequest, extra: Record<string, unknown> = {}): Response {
  return Response.json({
    protocol_version: 1,
    request_id: request.request_id,
    timestamp: new Date().toISOString(),
    ok: true,
    events: [],
    ...extra,
  });
}
function requestFrom(init: RequestInit | undefined): ProtocolRequest {
  return JSON.parse(String(init?.body)) as ProtocolRequest;
}
function latestSocket(): TestWebSocket {
  const socket = sockets.at(-1);
  if (!socket) throw new Error('No WebSocket was opened');
  return socket;
}
describe('BrowserBackendClient HTTP', () => {
  it('posts envelopes and preserves pending acknowledgments and readiness', async () => {
    const requests: ProtocolRequest[] = [];
    spyOn(fetchTarget, 'fetch').mockImplementation(async (url, init) => {
      expect(String(url)).toBe('http://localhost:8765/api/request');
      expect(init).toMatchObject({method: 'POST', headers: {'Content-Type': 'application/json'}});
      const request = requestFrom(init);
      requests.push(request);
      return response(request, {
        ack: {action: 'pause', status: 'pending'},
        experiments_ready: false,
      });
    });
    expect(
      await client.request({type: 'command.pause', mode: 'after_current_agent_call'}),
    ).toMatchObject({ack: {status: 'pending'}, experiments_ready: false});
    await client.request({type: 'query.snapshot'});
    expect(requests[0]).toMatchObject({
      protocol_version: 1,
      type: 'command.pause',
      mode: 'after_current_agent_call',
    });
    expect(requests[0]?.request_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(requests[0]?.request_id).not.toBe(requests[1]?.request_id);
    expect(Number.isNaN(Date.parse(requests[0]?.timestamp ?? ''))).toBe(false);
  });
  it('defaults to window origin and selects wss for HTTPS', async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'window');
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: {location: {origin: 'https://localhost:8765'}},
    });
    try {
      client = new BrowserBackendClient();
      const subscription = client.subscribe(0, noop, noop);
      expect(latestSocket().url).toBe('wss://localhost:8765/api/events');
      latestSocket().accept();
      await (await subscription).close();
    } finally {
      if (descriptor) Object.defineProperty(globalThis, 'window', descriptor);
      else Reflect.deleteProperty(globalThis, 'window');
    }
  });
  it('rejects non-HTTP URLs', () => {
    expect(() => new BrowserBackendClient('file:///tmp/run')).toThrow('http or https');
  });
  it('preserves structured errors on error HTTP statuses', async () => {
    const diagnostic = {
      code: 'not_ready',
      summary: 'Run is not attached',
      scope: 'request',
      retryability: 'manual',
    };
    for (const status of [200, 400]) {
      spyOn(fetchTarget, 'fetch').mockImplementation(
        async (_url, init) =>
          new Response(
            await response(requestFrom(init), {ok: false, error: 'not ready', diagnostic}).text(),
            {status},
          ),
      );
      const rejected = client.request({type: 'query.snapshot'});
      await expect(rejected).rejects.toBeInstanceOf(ServerError);
      await expect(rejected).rejects.toMatchObject({message: 'not ready', diagnostic});
    }
  });
  it.each([
    [{protocol_version: 2}, 'Unsupported server protocol version'],
    [{request_id: 'other'}, 'unexpected request ID'],
    [{ok: 'true'}, 'ok must be a boolean'],
  ])('validates envelopes %j', async (extra, error) => {
    spyOn(fetchTarget, 'fetch').mockImplementation(async (_url, init) =>
      response(requestFrom(init), extra),
    );
    await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(error);
  });
  it.each([
    [200, 'Invalid server response JSON'],
    [502, 'Server HTTP request failed: 502'],
  ])('rejects non-protocol HTTP responses %i', async (status, error) => {
    spyOn(fetchTarget, 'fetch').mockResolvedValue(
      new Response('<html>bad gateway</html>', {status}),
    );
    await expect(client.request({type: 'query.snapshot'})).rejects.toThrow(error);
  });
  it('times out requests while chat remains pending until close', async () => {
    client = new BrowserBackendClient('http://localhost:8765', {requestTimeoutMs: 5});
    const signals: AbortSignal[] = [];
    spyOn(fetchTarget, 'fetch').mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          if (!signal) throw new Error('Request must be cancellable');
          signals.push(signal);
          signal.addEventListener('abort', () => reject(signal.reason), {once: true});
        }),
    );
    const chat = client.request({type: 'query.chat', text: 'what happened?'}).catch(error => error);
    await expect(client.request({type: 'query.snapshot'})).rejects.toThrow('timed out after 5ms');
    expect(signals[0]?.aborted).toBe(false);
    expect(signals[1]?.aborted).toBe(true);
    await client.close();
    expect(await chat).toMatchObject({message: 'Server client is closed'});
  });
});
describe('BrowserBackendClient WebSocket', () => {
  it('preserves replay metadata and duplicate events', async () => {
    const messages: ServerMessage[] = [];
    const pending = client.subscribe(7, message => messages.push(message), noop, {tail: 1000});
    const socket = latestSocket();
    expect(socket.sent).toEqual([]);
    socket.accept();
    const subscription = await pending;
    expect(socket.sent[0]).toMatchObject({
      type: 'subscribe',
      protocol_version: 1,
      after_sequence: 7,
      tail: 1000,
    });
    const event = {sequence: 9, timestamp: '2026-01-01T00:00:00Z', type: 'server_started' as const};
    const batch: ServerMessage = {
      type: 'event_batch',
      events: [event],
      through_sequence: 10,
      active_executions: [],
      history_after_sequence: 8,
    };
    socket.receive(batch);
    socket.receive({type: 'event', event});
    expect(messages.slice(1)).toEqual([batch, {type: 'event', event}]);
    await subscription.close();
    expect(socket.closed).toBe(true);
  });
  it('keeps subscriptions independent and omits tail by default', async () => {
    const first = client.subscribe(0, noop, noop);
    const firstSocket = latestSocket();
    firstSocket.accept();
    const second = client.subscribe(12, noop, noop);
    const secondSocket = latestSocket();
    secondSocket.accept();
    expect(firstSocket.sent[0]).not.toHaveProperty('tail');
    await (await first).close();
    expect(secondSocket.closed).toBe(false);
    await (await second).close();
  });
  it('reports unexpected disconnect once and never for explicit close', async () => {
    const disconnected = mock(noop);
    const pending = client.subscribe(0, noop, disconnected);
    latestSocket().accept();
    await pending;
    latestSocket().onerror?.();
    await Promise.resolve();
    expect(disconnected).toHaveBeenCalledTimes(1);
    const second = client.subscribe(0, noop, disconnected);
    latestSocket().accept();
    await (await second).close();
    expect(disconnected).toHaveBeenCalledTimes(1);
  });
  it('delivers protocol errors without reporting a transport disconnect', async () => {
    const messages: ServerMessage[] = [];
    const disconnected = mock(noop);
    const pending = client.subscribe(0, message => messages.push(message), disconnected);
    latestSocket().accept();
    await pending;
    latestSocket().receive({
      type: 'protocol_error',
      code: 'stream_failed',
      message: 'stream failed',
    });
    expect(messages.at(-1)?.type).toBe('protocol_error');
    expect(disconnected).not.toHaveBeenCalled();
    expect(latestSocket().closed).toBe(true);
  });
  it('rejects protocol errors before subscription', async () => {
    const pending = client.subscribe(0, noop, noop);
    latestSocket().receive({type: 'protocol_error', code: 'not_ready', message: 'not ready'});
    await expect(pending).rejects.toMatchObject({name: 'ServerError', message: 'not ready'});
  });
  it.each([
    ['{broken', 'Invalid server event-stream message JSON'],
    [JSON.stringify({type: 'unknown'}), 'Unknown server event-stream message'],
    [
      JSON.stringify({
        type: 'subscribed',
        request_id: 'other',
        run_id: 'run-1',
        latest_sequence: 0,
      }),
      'unexpected request ID',
    ],
    [new ArrayBuffer(1), 'expected JSON text'],
  ])('rejects invalid subscription messages %s', async (data, error) => {
    const pending = client.subscribe(0, noop, noop);
    latestSocket().onmessage?.(new MessageEvent('message', {data}));
    await expect(pending).rejects.toThrow(error);
    expect(latestSocket().closed).toBe(true);
  });
  it('turns callback failure into one disconnect', async () => {
    const disconnected = mock(noop);
    const pending = client.subscribe(
      0,
      message => {
        if (message.type === 'event_batch') throw new Error('consumer failed');
      },
      disconnected,
    );
    latestSocket().accept();
    await pending;
    latestSocket().receive({type: 'event_batch', events: []});
    expect(disconnected).toHaveBeenCalledWith(
      expect.objectContaining({message: 'consumer failed'}),
    );
    expect(latestSocket().closed).toBe(true);
  });
  it('times out subscription handshake', async () => {
    client = new BrowserBackendClient('http://localhost:8765', {connectTimeoutMs: 5});
    await expect(client.subscribe(0, noop, noop)).rejects.toThrow(
      'subscription timed out after 5ms',
    );
    expect(latestSocket().closed).toBe(true);
  });
  it('closes pending and active subscriptions and prevents new work', async () => {
    const active = client.subscribe(0, noop, noop);
    latestSocket().accept();
    await active;
    const pending = client.subscribe(0, noop, noop).catch(error => error);
    await client.close();
    await client.close();
    expect(await pending).toMatchObject({message: 'Server client is closed'});
    expect(sockets.every(socket => socket.closed)).toBe(true);
    await expect(client.subscribe(0, noop, noop)).rejects.toThrow('Server client is closed');
  });
  it('resumes PersistentEventStream from caller cursor without tail', async () => {
    const stream = new PersistentEventStream(client, {tail: 1000, reconnectDelaysMs: [0]});
    const states: string[] = [];
    const resumed: boolean[] = [];
    const pending = stream.subscribe({
      cursor: () => 10,
      shouldReconnect: () => true,
      onMessage: (_message, context) => resumed.push(context.resumed),
      onConnectionState: state => states.push(state.status),
    });
    latestSocket().accept();
    await pending;
    latestSocket().receive({
      type: 'event_batch',
      events: [],
      through_sequence: 10,
      history_after_sequence: 8,
    });
    latestSocket().close();
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(sockets).toHaveLength(2);
    latestSocket().accept();
    await new Promise(resolve => setTimeout(resolve, 1));
    expect(latestSocket().sent[0]).toMatchObject({after_sequence: 10});
    expect(latestSocket().sent[0]).not.toHaveProperty('tail');
    expect(resumed).toEqual([false, false, true]);
    expect(states).toEqual(['disconnected', 'connected']);
    await stream.close();
  });
});
it('bundles browser export without Node runtime imports', async () => {
  const bundle = await Bun.build({
    entrypoints: [`${import.meta.dir}/browser.ts`],
    target: 'browser',
  });
  expect(bundle.success).toBe(true);
  expect(bundle.outputs).toHaveLength(1);
  expect(await bundle.outputs[0]?.text()).not.toMatch(/(?:node:|require\()/);
});
