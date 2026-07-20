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

    expect(controller.state.overlay?.kind).toBe('help');
    expect(controller.state.overlay?.content).toContain('/history');
    expect(controller.state.overlay?.content).toContain('Planned');
    expect(transport.requests).toEqual([]);
  });

  it('sends ordinary text as chat and renders the answer', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('what is happening?');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'what is happening?'}]);
    expect(controller.state.overlay?.content).toBe(
      'you: what is happening?\nvibesys: The implementer is running.',
    );
  });

  it('reduces replay and live events without depending on OpenTUI', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({type: 'event_batch', events: [event(1, 'agent_output_chunk', 'one\n')]});
    transport.emit({type: 'event', event: event(2, 'agent_output_chunk', 'two\n')});

    expect(controller.state.conversation.map(entry => entry.content).join('')).toBe('one\ntwo\n');
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
    expect(controller.state.overlay).toBeNull();
  });

  it('summarizes completed and running round durations', () => {
    const events = [
      {
        ...event(1, 'phase_started'),
        round_label: 'round-1-plan',
        timestamp: '2026-01-01T00:00:00Z',
        agent_kind: 'orchestrator',
        invocation_id: 'orchestrator-1',
      },
      {
        ...event(2, 'phase_finished'),
        round_label: 'round-1-plan',
        timestamp: '2026-01-01T00:00:20Z',
        agent_kind: 'orchestrator',
        invocation_id: 'orchestrator-1',
      },
      {
        ...event(3, 'phase_started'),
        round_label: 'round-1-implement',
        timestamp: '2026-01-01T00:00:10Z',
        agent_kind: 'implementer',
        invocation_id: 'implementer-1',
      },
      {
        ...event(4, 'phase_finished'),
        round_label: 'round-1-implement',
        timestamp: '2026-01-01T00:00:30Z',
        agent_kind: 'implementer',
        invocation_id: 'implementer-1',
      },
      {
        ...event(5, 'phase_started'),
        round_label: 'round-1-judge',
        timestamp: '2026-01-01T00:01:00Z',
        agent_kind: 'judge',
        invocation_id: 'judge-1',
      },
      {
        ...event(6, 'phase_finished'),
        round_label: 'round-1-judge',
        timestamp: '2026-01-01T00:01:15Z',
        agent_kind: 'judge',
        invocation_id: 'judge-1',
      },
      {...event(7, 'round_finished'), round_label: 'round-1', timestamp: '2026-01-01T00:02:05Z'},
      {
        ...event(8, 'phase_started'),
        round_label: 'round-2-plan',
        timestamp: '2026-01-01T00:03:00Z',
        agent_kind: 'implementer',
        invocation_id: 'implementer-1',
      },
    ];

    expect(renderRoundHistory(events, new Date('2026-01-01T00:03:42Z'))).toBe(
      [
        'Rounds',
        'Round 1 · completed · 45s · orchestrator -> implementer -> judge',
        'Round 2 · running · 42s · implementer',
      ].join('\n'),
    );
  });

  it('renders a performance curve from the perf command', async () => {
    const transport = new FakeTransport(
      [],
      [
        {
          round: 1,
          perf_metric: 1200,
          perf_unit: 'total_ops_per_sec',
          passed: true,
          profile_skipped: false,
        },
        {
          round: 2,
          perf_metric: 2400,
          perf_unit: 'total_ops_per_sec',
          passed: true,
          profile_skipped: false,
        },
      ],
    );
    const controller = new SocketSessionController(transport);

    await controller.submit('/perf');

    expect(transport.requests).toEqual([{type: 'query.performance'}]);
    expect(controller.state.overlay?.content).toContain('Performance · total_ops_per_sec');
    expect(controller.state.overlay?.content).toContain('best r2 2.4k total_ops_per_sec');
  });
});

class FakeTransport implements SupervisionTransport {
  closed = false;
  readonly requests: RequestInput[] = [];
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;

  constructor(
    private readonly responseEvents: RunEvent[] = [],
    private readonly responsePerformance: NonNullable<ProtocolResponse['performance']> = [],
  ) {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    return Promise.resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      events: this.responseEvents,
      performance: this.responsePerformance,
      ...(input.type === 'query.chat'
        ? {
            chat: {
              question: input.text,
              answer: 'The implementer is running.',
              effect: 'none' as const,
            },
          }
        : {}),
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
    ...(content === undefined
      ? {}
      : {
          data: {kind: 'agent_output_chunk', channel: 'assistant', content},
        }),
  };
}
