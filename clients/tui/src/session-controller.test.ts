import {describe, expect, it} from 'vitest';
import type {EventSubscription} from './client.js';
import type {ProtocolResponse, RequestInput, RunEvent, ServerMessage} from './protocol.js';
import {SocketSessionController, type SupervisionTransport} from './session-controller.js';

describe('session controller', () => {
  it('reduces replay and live events without depending on OpenTUI', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({type: 'event_batch', events: [event(1, 'agent_output_chunk', 'one\n')]});
    transport.emit({type: 'event', event: event(2, 'agent_output_chunk', 'two\n')});

    expect(controller.state.liveContent).toBe('one\ntwo\n');
    expect(controller.state.sequence).toBe(2);
    await controller.stop();
    expect(transport.closed).toBe(true);
  });

  it('keeps terminal state when the stream closes after completion', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({type: 'event', event: event(1, 'run_finished')});
    transport.disconnect(new Error('closed'));

    expect(controller.state.status).toBe('completed');
    expect(controller.state.view).toBe('live');
  });
});

class FakeTransport implements SupervisionTransport {
  closed = false;
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;

  request(_input: RequestInput): Promise<ProtocolResponse> {
    return Promise.resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      events: [],
      snapshot: {run_id: 'run', status: 'running', sequence: 12},
    });
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    this.#disconnect = onDisconnect;
    return Promise.resolve({close: async () => undefined});
  }

  close(): Promise<void> {
    this.closed = true;
    return Promise.resolve();
  }

  emit(message: ServerMessage): void {
    this.#message?.(message);
  }

  disconnect(error: Error): void {
    this.#disconnect?.(error);
  }
}

function event(sequence: number, type: RunEvent['type'], content?: string): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    ...(content === undefined ? {} : {
      data: {kind: 'agent_output_chunk', channel: 'assistant', content},
    }),
  };
}
