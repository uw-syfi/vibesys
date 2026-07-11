import {describe, expect, it} from 'vitest';
import type {EventSubscription} from './client.js';
import type {ProtocolResponse, RequestInput, RunEvent, ServerMessage} from './protocol.js';
import {
  renderRoundHistory,
  SocketSessionController,
  type SupervisionTransport,
} from './session-controller.js';

describe('session controller', () => {
  it('shows local help without sending a backend command', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('/help');

    expect(controller.state.view).toBe('help');
    expect(controller.state.detailContent).toContain('/history');
    expect(controller.state.detailContent).toContain('Planned');
    expect(transport.requests).toEqual([]);
  });

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

  it('summarizes completed and running round durations', () => {
    const events = [
      {...event(1, 'phase_started'), round_label: 'round-1-plan', timestamp: '2026-01-01T00:00:00Z'},
      {...event(2, 'round_finished'), round_label: 'round-1', timestamp: '2026-01-01T00:02:05Z'},
      {...event(3, 'phase_started'), round_label: 'round-2-plan', timestamp: '2026-01-01T00:03:00Z'},
    ];

    expect(renderRoundHistory(events, new Date('2026-01-01T00:03:42Z'))).toBe([
      'Rounds',
      'Round 1 · completed · 2m 5s',
      'Round 2 · running · 42s',
    ].join('\n'));
  });
});

class FakeTransport implements SupervisionTransport {
  closed = false;
  readonly requests: RequestInput[] = [];
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
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
