import {describe, expect, it} from 'bun:test';
import {type WebSocketLike, WebSocketTransport} from './websocket.js';

class FakeSocket implements WebSocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: {readonly data: unknown}) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readonly sent: string[] = [];
  closeCalls = 0;

  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  send(data: string): void {
    this.sent.push(data);
  }

  respond(message: unknown): void {
    this.onmessage?.({data: JSON.stringify(message)});
  }

  receive(data: unknown): void {
    this.onmessage?.({data});
  }

  close(): void {
    this.closeCalls += 1;
    this.readyState = 3;
    this.onclose?.();
  }
}

class FakeScheduler {
  readonly #entries: Array<{callback: () => void; cancelled: boolean}> = [];

  readonly schedule = (callback: () => void): (() => void) => {
    const entry = {callback, cancelled: false};
    this.#entries.push(entry);
    return () => {
      entry.cancelled = true;
    };
  };

  runPending(): void {
    for (const entry of this.#entries.splice(0)) {
      if (!entry.cancelled) entry.callback();
    }
  }
}

const response = (requestId: string, body: Record<string, unknown> = {}) => ({
  protocol_version: 1,
  request_id: requestId,
  ok: true,
  ...body,
});

describe('WebSocketTransport', () => {
  it('keeps control messages one text frame and correlates the response', async () => {
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport('ws://127.0.0.1:43123', {
      webSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        queueMicrotask(() => socket.open());
        return socket;
      },
    });

    const pending = transport.request({type: 'query.tui_defaults'});
    await tick();
    const frame = JSON.parse(sockets[0]?.sent[0] ?? '{}') as {request_id?: string};
    expect(sockets[0]?.sent[0]?.endsWith('\n')).toBe(false);
    sockets[0]?.respond(response(frame.request_id ?? '', {tui_defaults: {theme: 'default'}}));
    await expect(pending).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('delivers the subscribe acknowledgement and batch before resolving the handle', async () => {
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport('ws://127.0.0.1:43123', {
      webSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        queueMicrotask(() => socket.open());
        return socket;
      },
    });
    const messages: string[] = [];
    const subscription = transport.subscribe(
      0,
      message => messages.push(message.type ?? ''),
      error => {
        throw error;
      },
    );
    await tick();
    sockets[0]?.respond({
      type: 'subscribed',
      request_id: 'subscribe-1',
      run_id: 'run-1',
      latest_sequence: 2,
    });
    sockets[0]?.respond({type: 'event_batch', events: [], history_after_sequence: 0});
    await expect(subscription).resolves.toBeDefined();
    expect(messages).toEqual(['subscribed', 'event_batch']);
    await transport.close();
  });

  it('bounds the subscription handshake and closes an unresponsive socket', async () => {
    const scheduler = new FakeScheduler();
    const socket = new FakeSocket();
    const transport = new WebSocketTransport('ws://127.0.0.1:43123', {
      connectTimeoutMs: 10,
      scheduleTimeout: scheduler.schedule,
      webSocket: () => {
        queueMicrotask(() => socket.open());
        return socket;
      },
    });

    const subscription = transport.subscribe(
      0,
      () => {},
      () => {},
    );
    await tick();
    scheduler.runPending();

    await expect(subscription).rejects.toMatchObject({kind: 'timeout'});
    expect(socket.closeCalls).toBe(1);
    await transport.close();
  });

  it('rejects a structured subscription refusal without waiting for close', async () => {
    const socket = new FakeSocket();
    const transport = transportFor(socket);
    const messages: string[] = [];
    const subscription = transport.subscribe(
      0,
      message => messages.push(message.type ?? ''),
      () => {},
    );
    await tick();

    socket.respond({type: 'protocol_error', code: 'stream_failed', message: 'not available'});

    await expect(subscription).rejects.toMatchObject({kind: 'rejected', message: 'not available'});
    expect(messages).toEqual(['protocol_error']);
    expect(socket.closeCalls).toBe(1);
    await transport.close();
  });

  it('closes a subscribed stream after a malformed frame', async () => {
    const socket = new FakeSocket();
    const transport = transportFor(socket);
    const disconnects: Error[] = [];
    const subscription = transport.subscribe(
      0,
      () => {},
      error => disconnects.push(error),
    );
    await tick();
    socket.respond({
      type: 'subscribed',
      request_id: 'subscribe-1',
      run_id: 'run-1',
      latest_sequence: 0,
    });
    await subscription;

    socket.receive('{');

    expect(disconnects).toHaveLength(1);
    expect(disconnects[0]).toMatchObject({kind: 'parse'});
    expect(socket.closeCalls).toBe(1);
    await transport.close();
  });

  it('closes a corrupted control socket before redialing', async () => {
    const sockets: FakeSocket[] = [];
    const transport = new WebSocketTransport('ws://127.0.0.1:43123', {
      webSocket: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        queueMicrotask(() => socket.open());
        return socket;
      },
    });
    const first = transport.request({type: 'query.tui_defaults'});
    await tick();

    sockets[0]?.receive('{');
    await expect(first).rejects.toMatchObject({kind: 'parse'});
    expect(sockets[0]?.closeCalls).toBe(1);

    const second = transport.request({type: 'query.tui_defaults'});
    await tick();
    const frame = JSON.parse(sockets[1]?.sent[0] ?? '{}') as {request_id?: string};
    sockets[1]?.respond(response(frame.request_id ?? '', {tui_defaults: {theme: 'default'}}));
    await expect(second).resolves.toMatchObject({ok: true});
    await transport.close();
  });

  it('closes a socket still connecting and rejects its request', async () => {
    const socket = new FakeSocket();
    const transport = new WebSocketTransport('ws://127.0.0.1:43123', {
      webSocket: () => socket,
    });
    const pending = transport.request({type: 'query.tui_defaults'});

    await transport.close();

    await expect(pending).rejects.toMatchObject({kind: 'disconnected'});
    expect(socket.closeCalls).toBe(1);
  });

  it('does not report a disconnect when client close owns subscription teardown', async () => {
    const socket = new FakeSocket();
    const transport = transportFor(socket);
    const disconnects: Error[] = [];
    const subscription = transport.subscribe(
      0,
      () => {},
      error => disconnects.push(error),
    );
    await tick();
    socket.respond({
      type: 'subscribed',
      request_id: 'subscribe-1',
      run_id: 'run-1',
      latest_sequence: 0,
    });
    await subscription;

    await transport.close();

    expect(disconnects).toEqual([]);
    expect(socket.closeCalls).toBe(1);
  });
});

function transportFor(socket: FakeSocket): WebSocketTransport {
  return new WebSocketTransport('ws://127.0.0.1:43123', {
    webSocket: () => {
      queueMicrotask(() => socket.open());
      return socket;
    },
  });
}

async function tick(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
}
