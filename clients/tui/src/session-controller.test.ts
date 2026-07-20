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

  it('opens a multi-turn chat panel and renders agent answers there', async () => {
    const transport = new FakeTransport(
      [
        chatEvent(1, 'agent_output_chunk', {
          kind: 'agent_output_chunk',
          channel: 'analysis',
          content: 'Reading progress.md',
        }),
        chatEvent(2, 'tool_call', {
          kind: 'tool_call',
          tool: 'read_file',
          args: {path: 'progress.md'},
          status: null,
        }),
        chatEvent(3, 'chat', {
          kind: 'chat',
          answer: 'Round 2 improved throughput.',
        }),
      ],
      [],
      {
        question: 'what changed?',
        answer: 'Round 2 improved throughput.',
        effect: 'none',
      },
    );
    const controller = new SocketSessionController(transport);

    await controller.submit('/chat');

    expect(controller.state.chatOpen).toBe(true);
    expect(transport.requests).toEqual([]);

    await controller.sendChat('what changed?');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'what changed?'}]);
    expect(controller.state.chatConversation.map(entry => entry.kind)).toEqual([
      'user',
      'analysis',
      'tool',
      'assistant',
    ]);
    expect(controller.state.chatConversation.at(-1)?.content).toBe('Round 2 improved throughput.');

    controller.closeChat();
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.chatConversation).toHaveLength(4);
  });

  it('opens chat and sends an initial message from the command line', async () => {
    const transport = new FakeTransport([], [], {
      question: 'why?',
      answer: 'Because the configuration failed.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);

    await controller.submit('/chat why?');

    expect(controller.state.chatOpen).toBe(true);
    expect(transport.requests).toEqual([{type: 'query.chat', text: 'why?'}]);
  });

  it('queues messages entered while the chat agent is still working', async () => {
    const transport = new DeferredChatTransport();
    const controller = new SocketSessionController(transport);

    const first = controller.sendChat('first question');
    const second = controller.sendChat('follow-up question');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'first question'}]);
    expect(controller.state.chatConversation).toMatchObject([
      {kind: 'user', label: 'You', content: 'first question'},
      {kind: 'user', label: 'You · queued', content: 'follow-up question'},
    ]);

    transport.resolveNext('first answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests).toEqual([
      {type: 'query.chat', text: 'first question'},
      {type: 'query.chat', text: 'follow-up question'},
    ]);
    expect(controller.state.chatConversation[1]?.label).toBe('You');

    transport.resolveNext('follow-up answer');
    await Promise.all([first, second]);

    expect(controller.state.chatPending).toBe(false);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'first question',
      'follow-up question',
      'first answer',
      'follow-up answer',
    ]);
  });

  it('shows chat request failures as explicit failed trajectory entries', async () => {
    const transport = new FakeTransport([], [], undefined, new Error('Codex exited with code 1'));
    const controller = new SocketSessionController(transport);

    await controller.submit('/chat');
    await controller.sendChat('what happened?');

    expect(controller.state.chatPending).toBe(false);
    expect(controller.state.chatConversation.at(-1)).toMatchObject({
      kind: 'result',
      label: 'Chat failed',
      tone: 'failure',
      content: 'Error: Codex exited with code 1',
    });
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
    private readonly responseChat?: NonNullable<ProtocolResponse['chat']>,
    private readonly responseError?: Error,
  ) {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    if (this.responseError) return Promise.reject(this.responseError);
    return Promise.resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      events: this.responseEvents,
      performance: this.responsePerformance,
      ...(this.responseChat ? {chat: this.responseChat} : {}),
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

class DeferredChatTransport implements SupervisionTransport {
  readonly requests: RequestInput[] = [];
  readonly #pending: Array<(response: ProtocolResponse) => void> = [];

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    return new Promise(resolve => this.#pending.push(resolve));
  }

  resolveNext(answer: string): void {
    const resolve = this.#pending.shift();
    if (!resolve) throw new Error('No pending chat request');
    resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      chat: {
        question: '',
        answer,
        effect: 'none',
      },
    });
  }

  subscribe(
    _afterSequence: number,
    _onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    return Promise.resolve({close: async () => undefined});
  }

  close(): Promise<void> {
    return Promise.resolve();
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

function chatEvent(
  sequence: number,
  type: RunEvent['type'],
  data: NonNullable<RunEvent['data']>,
): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    agent_kind: 'chat',
    round_label: 'experiment-chat',
    invocation_id: 'chat-1',
    data,
  };
}
