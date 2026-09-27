import {describe, expect, it} from 'bun:test';
import {type WebSocketLike, WebSocketTransport} from './websocket.js';

class FakeSocket implements WebSocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: {readonly data: unknown}) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readonly sent: string[] = [];

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

  close(): void {
    this.readyState = 3;
    this.onclose?.();
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
});

function tick(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0));
}
