import {describe, expect, it} from 'bun:test';
import {
  type EventSubscription,
  type ProtocolResponse,
  type RequestInput,
  type RunEvent,
  ServerError,
  type ServerMessage,
  type SubscribeOptions,
} from '@vibesys/backend-client';
import {resolveStartupTrace} from './boot-trace.js';
import {type ServerTransport, SocketSessionController} from './session-controller.js';
import {chatPaneVisible, experimentLogVisible} from './session-model.js';

describe('session controller', () => {
  it('shows local help without sending a backend command', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/help');

    expect(controller.state.overlay?.kind).toBe('help');
    expect(controller.state.overlay?.content).toContain('/open-round');
    expect(controller.state.overlay?.content).not.toContain('Planned');
    expect(controller.state.overlay?.content).not.toContain('/invocation');
    expect(transport.requests).toEqual([]);
  });

  it('keeps ordinary text out of commands and accepts it from Experiment chat', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('what is happening?');

    expect(transport.requests).toEqual([]);
    expect(controller.state.chatConversation).toEqual([]);
    expect(controller.state.errorBanner?.message).toContain('Commands start with /');

    controller.dismissErrorBanner();
    await controller.submitChat('what is happening?');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'what is happening?'}]);
    // The pane is part of the landing view, so nothing opens over the table.
    expect(controller.state.chatOpen).toBe(false);
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(experimentLogVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation).toMatchObject([
      {kind: 'user', label: 'You', content: 'what is happening?'},
      {kind: 'assistant', label: 'Answer', content: 'The implementer is running.'},
    ]);
  });

  it('reduces replay and live events without depending on OpenTUI', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({type: 'event_batch', events: [event(1, 'agent_output_chunk', 'one\n')]});
    transport.emit({type: 'event', event: event(2, 'agent_output_chunk', 'two\n')});

    expect(controller.state.core.transcript.map(entry => entry.content).join('')).toBe(
      'one\ntwo\n',
    );
    expect(controller.state.core.sequence).toBe(2);
    await controller.stop();
    expect(transport.closed).toBe(true);
  });

  it('issues every boot request concurrently', async () => {
    const started: string[] = [];
    const pending: Array<() => void> = [];
    const transport: ServerTransport = {
      request(input: RequestInput): Promise<ProtocolResponse> {
        started.push(input.type ?? 'untyped');
        return new Promise(resolve => {
          pending.push(() =>
            resolve({
              protocol_version: 1,
              request_id: 'request',
              timestamp: '2026-01-01T00:00:00Z',
              ok: true,
            }),
          );
        });
      },
      subscribe(): Promise<EventSubscription> {
        started.push('subscribe');
        return new Promise(resolve => {
          pending.push(() => resolve({close: () => Promise.resolve()}));
        });
      },
      close: () => Promise.resolve(),
    };
    const controller = new SocketSessionController(transport);

    const boot = controller.start();

    // The event replay is the long pole; nothing waits behind it, and nothing
    // waits behind the two independent queries either.
    expect([...started].sort()).toEqual(['query.experiments', 'query.snapshot', 'subscribe']);
    for (const resolve of pending.splice(0)) resolve();

    // The design log deliberately rides behind the experiments answer rather
    // than the boot barrier, so it is the one follow-up request here.
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(started).toContain('query.design');
    for (const resolve of pending.splice(0)) resolve();
    await boot;
  });

  it('applies a replay batch before reconciling its active execution checkpoint', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event_batch',
      events: [
        {
          sequence: 1,
          timestamp: '2026-01-01T00:00:00Z',
          type: 'agent_execution_started',
          execution_id: 'stale-execution',
          agent_kind: 'implementer',
          round_label: 'round-1-implementer',
          data: {
            kind: 'agent_execution_started',
            stage: 'implementation',
            attempt: 1,
            system_prompt: '',
            user_prompt: 'Implement the queue',
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: 'Inspecting the queue',
              tool: null,
            },
          },
        },
        event(2, 'agent_output_chunk', 'persisted output\n'),
      ],
      through_sequence: 2,
      active_executions: [],
    });

    expect(controller.state.core.sequence).toBe(2);
    expect(controller.state.core.transcript.at(-1)?.content).toBe('persisted output\n');
    expect(controller.state.core.activeExecutions).toEqual({});
  });

  it('does not surface an old failure banner when a replay batch resumes running', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event_batch',
      events: [
        {
          ...event(1, 'run_failed'),
          diagnostic: {
            code: 'interrupted',
            summary: 'A previous process was interrupted.',
            scope: 'run',
            severity: 'fatal',
            retryability: 'never',
          },
        },
        {
          ...event(2, 'run_started'),
          data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
        },
      ],
      through_sequence: 2,
      active_executions: [],
    });

    expect(controller.state.core.status).toBe('running');
    expect(controller.state.errorBanner).toBeNull();
  });

  it('shows the final failure from a terminal event batch', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event_batch',
      events: [
        {
          ...event(1, 'run_failed'),
          diagnostic: {
            code: 'run_failed',
            summary: 'The current run failed.',
            scope: 'run',
            severity: 'fatal',
            retryability: 'never',
          },
        },
      ],
      through_sequence: 1,
      active_executions: [],
    });

    expect(controller.state.core.status).toBe('failed');
    expect(controller.state.errorBanner).toMatchObject({message: 'The current run failed.'});
  });

  it('keeps terminal state when the stream closes after completion', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({type: 'event', event: event(1, 'run_finished')});
    transport.disconnect(new Error('closed'));

    expect(controller.state.core.status).toBe('completed');
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.errorBanner).toBeNull();
  });

  it('preserves backend execution state but suppresses live activity after a disconnect', async () => {
    const transport = new FakeTransport();
    // An empty backoff schedule: this test is about the disconnected state
    // itself, not the reconnect that would otherwise follow.
    const controller = new SocketSessionController(transport, undefined, undefined, []);
    await controller.start();
    transport.emit({
      type: 'event',
      event: {
        sequence: 1,
        timestamp: '2026-01-01T00:00:00Z',
        type: 'agent_execution_started',
        execution_id: 'impl-1',
        agent_kind: 'implementer',
        round_label: 'round-1-implementer',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement the queue',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Inspecting the queue',
            tool: null,
          },
        },
      },
    });
    expect(controller.state.core.activeExecutions['impl-1']).toBeDefined();

    transport.disconnect(new Error('Server event stream disconnected'));

    expect(controller.state.core.activeExecutions['impl-1']).toBeDefined();
    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
  });

  it('prefers structured response and protocol diagnostics over legacy messages', async () => {
    const diagnostic = {
      id: 'request-1',
      code: 'unknown_future_code',
      summary: 'The requested operation was rejected.',
      detail: 'PermissionError: missing capability.',
      hint: 'Request the required capability.',
      scope: 'request' as const,
      severity: 'error' as const,
      retryability: 'manual' as const,
    };
    const transport = new FakeTransport(
      [],
      [],
      undefined,
      new ServerError('legacy request message', diagnostic),
    );
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/resume');

    expect(controller.state.errorBanner).toMatchObject({
      message: diagnostic.summary,
      detail: diagnostic.detail,
      hint: diagnostic.hint,
      diagnosticId: diagnostic.id,
      scope: 'request',
    });

    const protocolTransport = new FakeTransport();
    const protocolController = new SocketSessionController(protocolTransport);
    await protocolController.start();
    protocolTransport.emit({
      type: 'protocol_error',
      code: 'unknown_protocol_code',
      message: 'legacy protocol message',
      diagnostic: {...diagnostic, id: 'protocol-1', scope: 'protocol', severity: 'warning'},
    });

    expect(protocolController.state.errorBanner).toMatchObject({
      message: diagnostic.summary,
      diagnosticId: 'protocol-1',
      scope: 'protocol',
      severity: 'recoverable',
    });
    protocolTransport.disconnect(new Error('Server event stream disconnected'));
    expect(protocolController.state.errorBanner).toMatchObject({
      message: diagnostic.summary,
      diagnosticId: 'protocol-1',
      scope: 'protocol',
    });
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

    await controller.submitCommand('/perf');

    expect(transport.requests).toEqual([{type: 'query.performance'}]);
    // The chart lands beside the transcript, not over it.
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.title).toBe('Performance');
    expect(controller.state.layout.right?.content).toContain('Performance · total_ops_per_sec');
    expect(controller.state.layout.right?.content).toContain('best r2 2.4k total_ops_per_sec');
    expect(controller.state.layout.focus).toBe('right');
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

    await controller.submitCommand('/chat');

    // Already on screen: /chat puts the pane keys on it instead of opening a
    // modal over the log.
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.focus).toBe('chat');
    expect(transport.requests).toEqual([]);

    await controller.sendChat('what changed?');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'what changed?'}]);
    // The exchange, and only the exchange: the chat agent's own narration and
    // tool turns belong in the transcript, not on top of the answer.
    expect(controller.state.chatConversation.map(entry => entry.kind)).toEqual([
      'user',
      'assistant',
    ]);
    expect(controller.state.chatConversation.at(-1)?.content).toBe('Round 2 improved throughput.');

    controller.closeChat();
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.chatConversation).toHaveLength(2);
  });

  it('opens the chat as a modal where it cannot dock', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    // A terminal too narrow for two columns, reported by the renderer.
    controller.setChatDockFits(false);

    await controller.submitChat('what is happening?');

    expect(controller.state.chatOpen).toBe(true);
    expect(chatPaneVisible(controller.state)).toBe(false);
    expect(controller.state.chatConversation.at(-1)?.content).toBe('The implementer is running.');
  });

  it('keeps the log as the view when the chat opens over it', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);
    await controller.start();
    // Too narrow to dock, so the question opens the modal.
    controller.setChatDockFits(false);

    await controller.sendChat('what is happening?');

    expect(controller.state.chatOpen).toBe(true);
    // The modal floats over the table. It must not put the operator into the
    // per-round transcript they never asked for.
    expect(experimentLogVisible(controller.state)).toBe(true);
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
  });

  it('offers /chat in help only where the chat is not already on screen', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/help');
    expect(controller.state.overlay?.content).not.toMatch(/\/chat\s/);

    // Inside a hypothesis the chat is a dialog again, so the command returns.
    controller.enterExperimentDrilldown();
    await controller.submitCommand('/help');
    expect(controller.state.overlay?.content).toMatch(/\/chat\s/);
  });

  it('carries the modal conversation back into the docked pane', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.enterExperimentDrilldown();

    // Asked from the trajectory view, where the chat is a pop-up.
    await controller.submitCommand('/chat why did r1 fail?');
    expect(controller.state.chatOpen).toBe(true);

    controller.live();

    // Back on the landing view the same conversation is in the column, both
    // the question and what came back.
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'why did r1 fail?',
      'The implementer is running.',
    ]);
  });

  it('opens the chat as a modal inside a hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.enterExperimentDrilldown();

    await controller.sendChat('why did r1 fail?');

    // The row belongs to the transcript here, so the chat is the dialog it was.
    expect(controller.state.chatOpen).toBe(true);

    // Back on the landing view it docks again, transcript intact.
    controller.live();
    expect(controller.state.chatOpen).toBe(false);
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.at(0)?.content).toBe('why did r1 fail?');
  });

  it('opens chat and sends an initial message from the command line', async () => {
    const transport = new FakeTransport([], [], {
      question: 'why?',
      answer: 'Because the configuration failed.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/chat why?');

    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.focus).toBe('chat');
    expect(transport.requests).toEqual([{type: 'query.chat', text: 'why?'}]);
  });

  it('batches messages queued while the chat agent is still working', async () => {
    const transport = new DeferredChatTransport();
    const controller = new SocketSessionController(transport);

    const first = controller.sendChat('first question');
    const second = controller.sendChat('follow-up question');
    const third = controller.sendChat('one more detail');

    expect(transport.requests).toEqual([{type: 'query.chat', text: 'first question'}]);
    expect(controller.state.chatConversation).toMatchObject([
      {kind: 'user', label: 'You', content: 'first question'},
      {kind: 'user', label: 'You · queued', content: 'follow-up question'},
      {kind: 'user', label: 'You · queued', content: 'one more detail'},
    ]);

    transport.resolveNext('first answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests).toEqual([
      {type: 'query.chat', text: 'first question'},
      {type: 'query.chat', text: 'follow-up question\n\none more detail'},
    ]);
    expect(controller.state.chatConversation[1]?.label).toBe('You');
    expect(controller.state.chatConversation[2]?.label).toBe('You');

    transport.resolveNext('follow-up answer');
    await Promise.all([first, second, third]);

    expect(controller.state.chatPending).toBe(false);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'first question',
      'follow-up question',
      'one more detail',
      'first answer',
      'follow-up answer',
    ]);
  });

  it('starts a new batch for messages entered after a queued batch is sent', async () => {
    const transport = new DeferredChatTransport();
    const controller = new SocketSessionController(transport);

    const first = controller.sendChat('first');
    const second = controller.sendChat('second');
    const third = controller.sendChat('third');
    transport.resolveNext('first answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat', text: 'second\n\nthird'});

    const fourth = controller.sendChat('fourth');
    transport.resolveNext('batched answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat', text: 'fourth'});

    transport.resolveNext('fourth answer');
    await Promise.all([first, second, third, fourth]);
  });

  it('shows chat request failures as explicit failed trajectory entries', async () => {
    const transport = new FakeTransport([], [], undefined, new Error('Codex exited with code 1'));
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/chat');
    await controller.sendChat('what happened?');

    expect(controller.state.chatPending).toBe(false);
    expect(controller.state.chatConversation.at(-1)).toMatchObject({
      kind: 'result',
      label: 'Chat failed',
      tone: 'failure',
      content: 'Codex exited with code 1',
    });
  });

  it('starts on the requested theme and defaults to dark', () => {
    expect(new SocketSessionController(new FakeTransport()).state.themeName).toBe('dark');
    expect(
      new SocketSessionController(new FakeTransport(), 'catppuccin-latte').state.themeName,
    ).toBe('catppuccin-latte');
  });

  it('opens the theme list as a selection starting on the active theme', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport, 'solarized-dark');

    await controller.submitCommand('/theme');

    expect(controller.state.themePicker?.selected).toBe('solarized-dark');
    // The list is a selection, not a text overlay.
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.themeName).toBe('solarized-dark');
    expect(transport.requests).toEqual([]);
  });

  it('applies the selected theme and closes the picker', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme');
    controller.moveThemeSelection(2);
    controller.applySelectedTheme();

    expect(controller.state.themeName).toBe('solarized-dark');
    expect(controller.state.themePicker).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('closes the picker without switching when it is dismissed', async () => {
    const controller = new SocketSessionController(new FakeTransport(), 'light');

    await controller.submitCommand('/theme');
    controller.moveThemeSelection(1);
    controller.closeThemePicker();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
  });

  it('closes the picker when the selection is the theme already in use', async () => {
    const controller = new SocketSessionController(new FakeTransport(), 'light');

    await controller.submitCommand('/theme');
    controller.applySelectedTheme();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
  });

  it('switches theme locally and closes the picker', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme');
    await controller.submitCommand('/theme high-contrast-dark');

    expect(controller.state.themeName).toBe('high-contrast-dark');
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.overlay).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('makes the experiment log the landing view without a command', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.requests).toContainEqual({type: 'query.experiments'});
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
    expect(controller.state.overlay).toBeNull();
  });

  it('rejects the removed experiment-log commands without reaching the backend', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/history');
    expect(controller.state.errorBanner?.scope).toBe('input');
    expect(controller.state.errorBanner?.message).toContain('Unknown command: /history');

    await controller.submitCommand('/history rounds');
    expect(controller.state.errorBanner?.message).toContain('Unknown command: /history rounds');

    await controller.submitCommand('/experiments');
    expect(controller.state.errorBanner?.message).toContain('Unknown command: /experiments');

    controller.dismissErrorBanner();
    expect(controller.state.errorBanner).toBeNull();

    await controller.submitCommand('/history');
    expect(controller.state.errorBanner?.message).toContain('Unknown command: /history');

    expect(transport.requests).toEqual([]);
  });

  it('returns to the experiment log from a hypothesis without a command', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisDetail).not.toBeNull();
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisScope).not.toBeNull();

    // What Ctrl+L and Escape are bound to.
    controller.live();

    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
  });

  it('refetches the log when experiments change and keeps the selected row', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {resolved_outcome: 'proven'}),
      entry('H-02', 2, 2, {active: true}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.moveExperimentSelection(1);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
    const before = transport.requests.length;

    // The active hypothesis resolves and a new one opens above nothing.
    transport.experiments = [
      entry('H-01', 1, 1, {resolved_outcome: 'proven'}),
      entry('H-02', 2, 3, {resolved_outcome: 'rejected'}),
      entry('H-03', 4, 4, {active: true}),
    ];
    transport.emit({type: 'event', event: event(9, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    const refetches = transport.requests.slice(before).filter(r => r.type === 'query.experiments');
    expect(refetches).toHaveLength(1);
    expect(controller.state.experimentLog?.entries).toHaveLength(3);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
    expect(controller.state.experimentLog?.entries[1]?.resolved_outcome).toBe('rejected');
  });

  it('does not refetch the log for events that cannot change it', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    transport.emit({type: 'event', event: event(1, 'agent_output_chunk', 'noise\n')});
    transport.emit({type: 'event', event: event(2, 'tool_call')});
    transport.emit({type: 'event', event: event(3, 'phase_finished')});
    transport.emit({type: 'event', event: event(4, 'round_finished')});
    await Promise.resolve();

    expect(transport.requests).toHaveLength(before);
  });

  it('keeps the log as the root view, with no way to dismiss it', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 2, {
        resolved_outcome: 'proven',
        rounds: [
          {round: 1, passed: true, reviewed: true},
          {round: 2, passed: true, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    // Per-round output is reachable only by opening a hypothesis.
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisDetail).not.toBeNull();
    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisScope).not.toBeNull();

    // live() is the Ctrl+L path; it returns to the table rather than to an
    // unfiltered transcript.
    controller.live();
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
  });

  it('opens a hypothesis trajectory and returns with the selection intact', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {
        resolved_outcome: 'proven',
        rounds: [{round: 1, passed: true, reviewed: true}],
      }),
      entry('H-02', 2, 3, {
        resolved_outcome: 'rejected',
        rounds: [
          {round: 2, passed: false, reviewed: false},
          {round: 3, passed: false, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.openExperimentLog();
    controller.moveExperimentSelection(1);

    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisDetail).toEqual({entryKey: 'H-02', selectedRound: 3});
    expect(controller.state.hypothesisScope).toBeNull();

    controller.enterExperimentDrilldown();
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.hypothesisScope?.label).toBe('H-02 · r2-3');

    controller.leaveExperimentDrilldown();
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.hypothesisDetail).toEqual({entryKey: 'H-02', selectedRound: 3});
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
  });

  it('loads the log before the first frame so it can be the landing view', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    expect(controller.state.experimentLog?.pending).toBe(true);
    await controller.start();

    expect(transport.requests).toEqual([
      {type: 'query.snapshot'},
      {type: 'query.experiments'},
      {type: 'query.design'},
    ]);
    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
  });

  it('loads the design log with the experiments so the drill-down can annotate rounds', async () => {
    const transport = new FakeTransport();
    transport.design = [{round: 1, files: [{path: 'src/ring.rs', change: 'added'}]}];
    const controller = new SocketSessionController(transport);

    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.designLog).toEqual(transport.design);
  });

  it('leaves the design log unloaded until the backend reports it ready', async () => {
    const transport = new FakeTransport();
    transport.designReady = false;
    const controller = new SocketSessionController(transport);

    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.designLog).toBeNull();
  });

  it('renders /design in the right pane, joining stage facts by round', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {
        title: 'Pad the ring indices',
        rounds: [{round: 1, passed: true, reviewed: true, judge_verdict: 'pass'}],
      }),
    ];
    transport.design = [
      {
        round: 1,
        files: [
          {path: 'src/ring.rs', change: 'added'},
          {path: 'src/lib.rs', change: 'modified'},
        ],
      },
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    await controller.submitCommand('/design');

    expect(controller.state.overlay).toBeNull();
    expect(controller.state.layout.right?.view).toBe('design');
    expect(controller.state.layout.right?.title).toBe('Design changes');
    expect(controller.state.layout.right?.content).toContain('Design changes by round');
    expect(controller.state.layout.right?.content).toContain(
      'Round 1 · H-01 · Pad the ring indices',
    );
    expect(controller.state.layout.right?.content).toContain('src/ring.rs, src/lib.rs');
    expect(controller.state.designLog).toEqual(transport.design);
  });

  it('says the design log is not ready instead of showing an empty pane', async () => {
    const transport = new FakeTransport();
    transport.designReady = false;
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/design');

    expect(controller.state.layout.right?.content).toContain(
      'not available until a run is attached',
    );
    expect(controller.state.designLog).toBeNull();
  });

  it('keeps bootstrap pending until attached experiments become ready', async () => {
    const transport = new FakeTransport();
    transport.experimentsReady = false;
    const controller = new SocketSessionController(transport);
    await controller.start();

    expect(controller.state.experimentLog?.pending).toBe(true);
    expect(controller.state.experimentLog?.entries).toEqual([]);

    transport.experiments = [entry('H-resumed', 1, 1, {active: true})];
    transport.experimentsReady = true;
    transport.emit({type: 'event', event: event(1, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.selectedId).toBe('H-resumed');
  });

  it('reports how long the landing view waited for experiments', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const traced: string[] = [];
    const controller = new SocketSessionController(transport, undefined, line => traced.push(line));

    await controller.start();

    expect(traced).toHaveLength(1);
    expect(traced[0]).toMatch(/^experiments loaded in \d+ms \(1 entries\)$/);
  });

  it('times the whole wait across a closed gate, and reports it once', async () => {
    const transport = new FakeTransport();
    transport.experimentsReady = false;
    const traced: string[] = [];
    const controller = new SocketSessionController(transport, undefined, line => traced.push(line));

    await controller.start();
    expect(traced).toEqual([]);

    transport.experiments = [entry('H-resumed', 1, 1, {active: true}), entry('H-02', 2, 2, {})];
    transport.experimentsReady = true;
    transport.emit({type: 'event', event: event(1, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    expect(traced).toEqual([expect.stringMatching(/^experiments loaded in \d+ms \(2 entries\)$/)]);

    // A later refresh is not a boot cost, so it does not report again.
    transport.emit({type: 'event', event: event(2, 'experiments_changed')});
    await Promise.resolve();
    await Promise.resolve();

    expect(traced).toHaveLength(1);
  });

  it('stays silent through the real sink unless the boot trace is switched on', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const written: string[] = [];
    const controller = new SocketSessionController(
      transport,
      undefined,
      resolveStartupTrace({VIBESYS_LAUNCH_START_MS: String(Date.now() - 25)}, line =>
        written.push(line),
      ),
    );

    await controller.start();

    expect(written).toEqual([]);
  });

  it('writes one anchored line through the real sink when the boot trace is on', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const written: string[] = [];
    const controller = new SocketSessionController(
      transport,
      undefined,
      resolveStartupTrace(
        {VIBESYS_BOOT_TRACE: '1', VIBESYS_LAUNCH_START_MS: String(Date.now() - 25)},
        line => written.push(line),
      ),
    );

    await controller.start();

    expect(written).toHaveLength(1);
    expect(written[0]).toMatch(/^experiments loaded in \d+ms \(1 entries\); \d+ms since launch$/);
  });

  it('omits the since-launch suffix when the launch anchor is absent', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const written: string[] = [];
    const controller = new SocketSessionController(
      transport,
      undefined,
      resolveStartupTrace({VIBESYS_BOOT_TRACE: '1'}, line => written.push(line)),
    );

    await controller.start();

    expect(written).toHaveLength(1);
    expect(written[0]).toMatch(/^experiments loaded in \d+ms \(1 entries\)$/);
    expect(written[0]).not.toContain('since launch');
  });

  it('coalesces refetches when a burst of experiment changes lands', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    transport.emit({
      type: 'event_batch',
      events: [event(1, 'experiments_changed'), event(2, 'experiments_changed')],
    });
    await Promise.resolve();
    await Promise.resolve();

    const refetches = transport.requests.slice(before).filter(r => r.type === 'query.experiments');
    expect(refetches).toHaveLength(1);
  });

  it('refetches again when experiments change during an in-flight fetch', async () => {
    const transport = new DeferredExperimentsTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit(event(1, 'experiments_changed'));
    expect(transport.experimentRequests).toBe(2);

    transport.emit(event(2, 'experiments_changed'));
    transport.emit(event(3, 'experiments_changed'));
    transport.resolveExperiment([entry('H-stale', 1, 1, {active: true})]);
    // A macrotask, because the design refresh sits between the answer and the
    // queued refetch and its length is not this test's concern.
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentRequests).toBe(3);
    transport.resolveExperiment([entry('H-current', 1, 2, {resolved_outcome: 'proven'})]);
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.selectedId).toBe('H-current');
    expect(transport.experimentRequests).toBe(3);
  });

  it('opens the selected hypothesis with /open-round', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
      entry('H-02', 2, 3, {
        rounds: [
          {round: 2, passed: false, reviewed: false},
          {round: 3, passed: false, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.moveExperimentSelection(1);

    await controller.submitCommand('/open-round');

    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    // Lands on the hypothesis's latest round: the round view is built around
    // one round, and `[` walks back through the earlier ones.
    expect(controller.state.selectedRound).toBe(3);
  });

  it('opens the hypothesis owning a round with /open-round --N', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
      entry('H-02', 2, 3, {
        rounds: [
          {round: 2, passed: false, reviewed: false},
          {round: 3, passed: false, reviewed: true},
        ],
      }),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/open-round --3');

    // Lands on the requested round, inside the hypothesis that owns it, and
    // moves the table selection to match.
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.selectedRound).toBe(3);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
  });

  it('opens a recorded round that belongs to no hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit({
      type: 'event',
      event: {...event(1, 'phase_started'), agent_kind: 'orchestrator', round_label: 'round-9-pre'},
    });

    await controller.submitCommand('/open-round --9');

    expect(controller.state.hypothesisScope).toMatchObject({id: 'round-9', rounds: [9]});
    expect(controller.state.selectedRound).toBe(9);
  });

  it('opens the first planning activity before it has a hypothesis record', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({
      type: 'event',
      event: {...event(1, 'phase_started'), agent_kind: 'orchestrator', round_label: 'round-1-pre'},
    });

    controller.enterExperimentDrilldown();

    expect(controller.state.hypothesisScope).toMatchObject({id: 'round-1', rounds: [1]});
    expect(controller.state.selectedRound).toBe(1);
  });

  it('reports a round that has not been observed', async () => {
    const controller = new SocketSessionController(new FakeTransport());

    await controller.submitCommand('/open-round --9');

    expect(controller.state.overlay?.content).toContain('Round 9 has not been recorded.');
    expect(controller.state.hypothesisScope).toBeNull();
  });

  it('says where it already is when /open-round runs inside a hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/open-round');

    await controller.submitCommand('/open-round');

    expect(controller.state.overlay?.content).toContain('Already inside H-01');
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-01'});
  });

  it('keeps the open pane current as rounds land', async () => {
    const transport = new FakeTransport(
      [],
      [{round: 1, perf_metric: 1200, perf_unit: 'ops', passed: true, profile_skipped: false}],
    );
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    const before = perfRequests(transport);

    transport.emit({type: 'event', event: event(9, 'round_finished')});
    await Promise.resolve();
    await Promise.resolve();

    // The experiment log refetches on the same event; count only the pane's.
    expect(perfRequests(transport) - before).toBe(1);
  });

  it('does not refetch the pane once it is closed', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    controller.closePane();
    const before = perfRequests(transport);

    transport.emit({type: 'event', event: event(9, 'round_finished')});
    await Promise.resolve();

    expect(perfRequests(transport)).toBe(before);
    expect(controller.state.layout.right).toBeNull();
  });

  it('closes the pane without disturbing the chat', async () => {
    const transport = new FakeTransport([], [], {
      question: 'why?',
      answer: 'Round 2 regressed.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.sendChat('why?');
    await controller.submitCommand('/perf');

    controller.closePane();

    expect(controller.state.layout.right).toBeNull();
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'why?',
      'Round 2 regressed.',
    ]);
  });

  it('keeps the docked chat beside the log while a visualization is open', async () => {
    const transport = new FakeTransport(
      [],
      [{round: 1, perf_metric: 1200, perf_unit: 'ops', passed: true, profile_skipped: false}],
    );
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/perf');
    await controller.sendChat('what changed in r1?');

    // Three columns: chat, log, pane. None of them replaced another.
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(chatPaneVisible(controller.state)).toBe(true);
    expect(experimentLogVisible(controller.state)).toBe(true);
    expect(controller.state.chatConversation.at(0)?.content).toBe('what changed in r1?');
  });

  it('sends chat messages while the pane stays put', async () => {
    const transport = new FakeTransport([], [], {
      question: 'what regressed?',
      answer: 'The sampler reorder.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    const pane = controller.state.layout.right;

    await controller.sendChat('what regressed?');

    expect(controller.state.chatConversation.at(-1)?.content).toBe('The sampler reorder.');
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.content).toBe(pane?.content);
  });

  it('surfaces a failed experiment query without closing the view', async () => {
    const transport = new FakeTransport([], [], undefined, new Error('socket closed'));
    const controller = new SocketSessionController(transport);

    await controller.openExperimentLog();

    expect(controller.state.experimentLog?.error).toContain('socket closed');
    expect(controller.state.experimentLog?.pending).toBe(false);
  });

  it('runs a slash command typed in the chat through the main input path', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.submitCommand('/chat');
    const before = transport.requests.length;

    // The performance plot, which is the command that answers in the right
    // pane now that the experiment log is reached without a command.
    await controller.submitChat('/perf');

    // Handled as a command, not forwarded to the chat agent.
    expect(transport.requests.slice(before)).toEqual([{type: 'query.performance'}]);
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.content).toContain('No performance data yet.');
    expect(controller.state.chatConversation).toHaveLength(0);
  });

  it('shows the chat help for an unknown slash command instead of asking the agent', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitChat('/nope');

    // Chat-scoped help, not the global surface's "unknown command" banner.
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.chatConversation.at(-1)?.content).toContain('/clear');
    expect(transport.requests).toEqual([]);
  });

  it('still sends ordinary questions, including text containing a slash', async () => {
    const transport = new FakeTransport([], [], {
      question: 'what changed in a/b testing?',
      answer: 'Nothing yet.',
      effect: 'none',
    });
    const controller = new SocketSessionController(transport);

    await controller.submitChat('what changed in a/b testing?');

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat',
      text: 'what changed in a/b testing?',
    });
    expect(controller.state.chatConversation.at(-1)?.content).toBe('Nothing yet.');
  });

  it('renders /model from the backend options, grouped by harness', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat_options'});
    const menu = controller.state.chatMenu;
    expect(menu?.kind).toBe('model');
    expect(menu?.pending).toBe(false);
    // Exactly what the backend returned: harness groups, their models, and one
    // free-text entry per group. The client enumerates nothing of its own.
    expect(menu?.rows.map(row => [row.kind, row.label])).toEqual([
      ['header', 'Codex'],
      ['model', 'gpt-run  \u00b7 run default'],
      ['model', 'gpt-5.6-sol'],
      ['custom', 'custom model\u2026'],
      ['header', 'Claude Code'],
      ['model', 'claude-opus-5'],
      ['custom', 'custom model\u2026'],
    ]);
    // Headers are structure, so the highlight starts on the first real choice.
    expect(menu?.selected).toBe(1);
  });

  it('starts a thread on the selected model, sending no driver', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');
    controller.moveChatMenuSelection(1);
    await controller.confirmChatMenu();

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat_thread_create',
      provider: 'codex',
      model: 'gpt-5.6-sol',
    });
    expect(controller.state.chatMenu).toBeNull();
    // The thread record comes from the replayed backend event, and the
    // client switches the chat surfaces to it.
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toEqual([
      'default',
      'thread-1',
    ]);
    expect(controller.state.activeChatThreadId).toBe('thread-1');
  });

  it('accepts a typed model from a group\u2019s custom entry', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');
    // Down to the Claude group's custom entry, skipping the group headers.
    controller.moveChatMenuSelection(4);
    expect(controller.state.chatMenu?.rows[controller.state.chatMenu.selected]).toMatchObject({
      kind: 'custom',
      provider: 'claude',
    });
    for (const character of 'claude-sonnet-5') controller.typeChatMenuCustomModel(character);
    controller.backspaceChatMenuCustomModel();
    controller.typeChatMenuCustomModel('5');
    await controller.confirmChatMenu();

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat_thread_create',
      provider: 'claude',
      model: 'claude-sonnet-5',
    });
    expect(controller.state.activeChatThreadId).toBe('thread-1');
  });

  it('leaves an empty custom entry alone rather than guessing a model', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');
    controller.moveChatMenuSelection(2);
    await controller.confirmChatMenu();

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat_options'});
    expect(controller.state.chatMenu?.kind).toBe('model');
  });

  it('reports a chat-options failure in the menu instead of an empty list', async () => {
    const transport = new ThreadTransport();
    transport.chatOptions = null;
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/model');

    expect(controller.state.chatMenu?.error).toContain('has not reported its chat options');
    expect(controller.state.chatMenu?.selected).toBe(-1);
  });

  it('/clear starts a fresh thread on the current thread\u2019s settings', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitChat('/model');
    controller.moveChatMenuSelection(1);
    await controller.confirmChatMenu();
    await controller.sendChat('what changed?');

    await controller.submitChat('/clear');

    // Same harness and model, a new thread, and the old one still listed.
    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat_thread_create',
      provider: 'codex',
      model: 'gpt-5.6-sol',
    });
    expect(controller.state.activeChatThreadId).toBe('thread-2');
    expect(controller.state.core.chatThreads.map(thread => thread.id)).toEqual([
      'default',
      'thread-1',
      'thread-2',
    ]);
    // The cleared thread keeps its transcript, so /resume gets it back intact.
    expect(controller.state.chatConversations['thread-1']?.map(item => item.content)).toEqual([
      'what changed?',
      'Thread answer.',
    ]);
    expect(controller.state.chatConversation).toEqual([]);
  });

  it('/clear on the default thread lets the backend resolve the run settings', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/clear');

    expect(transport.requests.at(-1)).toEqual({type: 'query.chat_thread_create'});
  });

  it('answers unknown slash input in the composer with the chat help', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    await controller.submitChat('/threads');

    // No request at all, and no global "unknown command" error banner.
    expect(transport.requests.length).toBe(before);
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.chatConversation.at(-1)?.content).toContain('/model');
  });

  it('still forwards a global command typed into the composer', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/pause');

    expect(transport.requests.at(-1)).toEqual({type: 'command.pause'});
  });

  it('sends chat to the active thread and keeps transcripts apart', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitChat('/model');
    await controller.confirmChatMenu();
    expect(controller.state.activeChatThreadId).toBe('thread-1');

    await controller.sendChat('which kernel changed?');

    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat',
      text: 'which kernel changed?',
      thread_id: 'thread-1',
    });
    expect(controller.state.chatConversations['thread-1']?.map(entry => entry.content)).toEqual([
      'which kernel changed?',
      'Thread answer.',
    ]);
    expect(controller.state.chatConversations['default'] ?? []).toEqual([]);

    // Switching swaps what the singular selectors show; nothing is lost.
    controller.switchChatThread('default');
    expect(controller.state.chatConversation).toEqual([]);
    await controller.sendChat('and the default thread?');
    expect(transport.requests.at(-1)).toEqual({
      type: 'query.chat',
      text: 'and the default thread?',
    });
    controller.switchChatThread('thread-1');
    expect(controller.state.chatConversation.map(entry => entry.content)).toEqual([
      'which kernel changed?',
      'Thread answer.',
    ]);
  });

  it('/switch lists the threads with their runtime and switches', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitChat('/model');
    await controller.confirmChatMenu();
    controller.switchChatThread('default');

    await controller.submitChat('/switch');

    const menu = controller.state.chatMenu;
    expect(menu?.kind).toBe('resume');
    expect(menu?.rows.map(row => [row.kind, row.label])).toEqual([
      ['thread', 'Experiment chat'],
      ['thread', 'Codex (GPT Run)'],
    ]);
    // The runtime is spelled out beside each thread, harness and model only.
    expect(menu?.rows.map(row => (row.kind === 'thread' ? row.detail : null))).toEqual([
      'run agent',
      'Codex (GPT Run)',
    ]);
    // The highlight starts on the thread that is currently on screen.
    expect(menu?.selected).toBe(0);

    controller.moveChatMenuSelection(1);
    await controller.confirmChatMenu();

    expect(controller.state.chatMenu).toBeNull();
    expect(controller.state.activeChatThreadId).toBe('thread-1');
  });

  it('resumes the paused run from the chat, same as the command bar', async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitChat('/resume');

    // The chat forwards /resume to the one command executor: it resumes the run
    // and opens no thread menu.
    expect(transport.requests).toContainEqual({type: 'command.resume'});
    expect(controller.state.chatMenu).toBeNull();
  });

  it("reports a chat-only command's usage error rather than an unknown-command error", async () => {
    const transport = new ThreadTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    // /clear takes no argument. The chat resolves it, so the error must be the
    // registry usage error; re-parsing the text on the command bar, which does
    // not offer /clear, would report "Unknown command" instead.
    const before = transport.requests.length;
    await controller.submitChat('/clear definitely-not');

    expect(controller.state.errorBanner?.message).toBe('Usage: /clear');
    // The phrase started no thread and sent no request.
    expect(transport.requests.length).toBe(before);
    expect(controller.state.chatMenu).toBeNull();
  });

  it('does not run a no-argument command typed with a trailing phrase', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/pause typo');

    expect(controller.state.errorBanner).toMatchObject({scope: 'input', message: 'Usage: /pause'});
    expect(transport.requests).toEqual([]);
  });

  it('reports an unknown theme as an error without switching', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme monokai');

    expect(controller.state.errorBanner).toMatchObject({scope: 'input'});
    expect(controller.state.errorBanner?.message).toContain('Unknown theme: monokai');
    expect(controller.state.themeName).toBe('dark');
    expect(transport.requests).toEqual([]);
  });

  it('boots against the tail of the stream rather than the whole history', async () => {
    const transport = new HistoryTransport();
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.subscribeTails).toEqual([1_000]);
  });

  it('falls back once to a full subscription when the tail is rejected', async () => {
    const history = [
      event(1, 'agent_output_chunk', 'one\n'),
      event(2, 'agent_output_chunk', 'two\n'),
    ];
    const transport = new HistoryTransport(history);
    transport.rejectTail = true;
    const controller = new SocketSessionController(transport);

    await controller.start();

    // The rejection is the capability probe, so there is exactly one retry and
    // it carries no tail.
    expect(transport.subscribeTails).toEqual([1_000, undefined]);

    transport.emitBatch(history, 0);

    expect(controller.state.core.transcript.map(item => item.content).join('')).toBe('one\ntwo\n');
    expect(controller.state.core.historyAfterSequence).toBe(0);
    // A boot that recovered is not a boot that failed.
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.eventStreamAvailable).toBe(true);
  });

  it('reports the transport error when the full subscription fails too', async () => {
    const transport = new HistoryTransport();
    transport.rejectTail = true;
    transport.subscribeError = new Error('Server is disconnected');
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.subscribeTails).toEqual([1_000, undefined]);
    expect(controller.state.eventStreamAvailable).toBe(false);
    expect(controller.state.errorBanner).toMatchObject({scope: 'transport'});
  });

  it('loads older history on demand while the fold is only a suffix', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitBatch(history.slice(1_500), 1_500);

    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    expect(controller.state.core.transcript).toHaveLength(500);

    await expect(controller.loadOlderHistory()).resolves.toBe(true);

    expect(eventsQueries(transport)).toEqual([
      {type: 'query.events', after_sequence: 500, before_sequence: 1_501},
    ]);
    expect(controller.state.core.historyAfterSequence).toBe(500);
    expect(controller.state.core.transcript).toHaveLength(1_500);
  });

  it('stops asking once the history floor reaches the start of the run', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);

    await controller.loadOlderHistory();
    await expect(controller.loadOlderHistory()).resolves.toBe(true);

    expect(controller.state.core.historyAfterSequence).toBe(0);
    expect(controller.state.core.transcript).toHaveLength(2_000);
    const issued = eventsQueries(transport).length;

    await expect(controller.loadOlderHistory()).resolves.toBe(false);

    expect(eventsQueries(transport)).toHaveLength(issued);

    // Every batch of the subscription repeats the floor it bootstrapped with,
    // which must not undo the backfill and send the client back for history it
    // already holds.
    transport.emitBatch([event(2_001, 'agent_output_chunk', 'live\n')], 1_500);

    expect(controller.state.core.historyAfterSequence).toBe(0);
    await expect(controller.loadOlderHistory()).resolves.toBe(false);
    expect(eventsQueries(transport)).toHaveLength(issued);
  });

  it('leaves the history floor where it was when a backfill fails', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);
    transport.eventsError = new Error('The event store is unavailable');

    await expect(controller.loadOlderHistory()).resolves.toBe(false);

    expect(controller.state.core.historyAfterSequence).toBe(1_500);
    expect(controller.state.errorBanner).toMatchObject({scope: 'request'});
    expect(controller.state.errorBanner?.message).toContain('The event store is unavailable');

    // The unchanged floor is what makes the same range retryable.
    transport.eventsError = null;
    await expect(controller.loadOlderHistory()).resolves.toBe(true);
    expect(controller.state.core.historyAfterSequence).toBe(500);
  });

  it('backfills once under concurrent requests', async () => {
    const history = longHistory(2_000);
    const transport = new HistoryTransport(history);
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(history.slice(1_500), 1_500);
    transport.deferEvents = true;

    const first = controller.loadOlderHistory();
    const second = controller.loadOlderHistory();

    expect(eventsQueries(transport)).toHaveLength(1);

    transport.releaseEvents();

    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    expect(controller.state.core.historyAfterSequence).toBe(500);
  });

  it('folds a spine event re-delivered by a backfill exactly once', async () => {
    // A tail subscription replays the run-level spine from below the floor, so
    // the backfill covering that range delivers those events a second time.
    const firstRound = roundFinished(2, 1);
    const secondRound = roundFinished(4, 2);
    const tail = event(5, 'agent_output_chunk', 'five\n');
    const history = [
      event(1, 'agent_output_chunk', 'one\n'),
      firstRound,
      event(3, 'agent_output_chunk', 'three\n'),
      secondRound,
      tail,
    ];
    const replayedTransport = new HistoryTransport(history);
    const replayed = new SocketSessionController(replayedTransport);
    await replayed.start();
    replayedTransport.emitBatch(history, 0);

    const tailedTransport = new HistoryTransport(history);
    const tailed = new SocketSessionController(tailedTransport);
    await tailed.start();
    // The spine (both `round_finished` events) below the floor, then the tail.
    tailedTransport.emitBatch([firstRound, secondRound, tail], 4);
    await tailed.loadOlderHistory();

    expect(tailed.state.core.historyAfterSequence).toBe(0);
    expect(tailed.state.core.transcript).toEqual(replayed.state.core.transcript);
    expect(tailed.state.core.rounds).toEqual(replayed.state.core.rounds);
  });
});

/**
 * The run's durable event log is attached after the client subscribes, so the
 * subscription's first batch comes from the server's own short log and the
 * stream then re-bootstraps at a tail of the run log. The two batches number
 * different logs, and the second declares a floor above the first.
 */
describe('a stream that re-bootstraps at a raised floor', () => {
  /** The run log, whose last event is the pre-attach one carried into it. */
  const runLog: RunEvent[] = [
    {
      ...event(1, 'run_started'),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    event(2, 'agent_output_chunk', 'two\n'),
    roundFinished(3, 1),
    event(4, 'agent_output_chunk', 'four\n'),
    event(5, 'agent_output_chunk', 'five\n'),
    event(6, 'agent_output_chunk', 'server started\n'),
  ];
  /** What the stream sends once the run log is attached: spine, then tail. */
  const rebootstrap = [runLog[0], runLog[2], runLog[4], runLog[5]] as RunEvent[];
  const preAttach = [event(1, 'agent_output_chunk', 'server started\n')];

  async function rebootstrapped(transport: HistoryTransport): Promise<SocketSessionController> {
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(preAttach, 0);
    transport.emitBatch(rebootstrap, 4);
    return controller;
  }

  it('keeps the raised floor so the skipped history is still backfillable', async () => {
    const transport = new HistoryTransport(runLog);

    const controller = await rebootstrapped(transport);

    expect(controller.state.core.historyAfterSequence).toBe(4);
    await expect(controller.loadOlderHistory()).resolves.toBe(true);
    expect(eventsQueries(transport)).toEqual([
      {type: 'query.events', after_sequence: 0, before_sequence: 5},
    ]);
  });

  it('folds the spine the pre-attach cursor would otherwise have swallowed', async () => {
    const controller = await rebootstrapped(new HistoryTransport(runLog));

    // `run_started` and `round_finished` sit at or below the cursor the
    // superseded log left behind, in a sequence space that no longer applies.
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.outerLoop).toBe('agent');
    expect(controller.state.core.rounds.map(round => round.number)).toEqual([1]);
  });

  it('reaches the state a full replay of the run log would have built', async () => {
    const replayedTransport = new HistoryTransport(runLog);
    const replayed = new SocketSessionController(replayedTransport);
    await replayed.start();
    replayedTransport.emitBatch(runLog, 0);

    const controller = await rebootstrapped(new HistoryTransport(runLog));
    await controller.loadOlderHistory();

    expect(controller.state.core.historyAfterSequence).toBe(0);
    expect(controller.state.core.transcript).toEqual(replayed.state.core.transcript);
    expect(controller.state.core.rounds).toEqual(replayed.state.core.rounds);
    expect(controller.state.core.sequence).toBe(replayed.state.core.sequence);
  });

  it('refreshes experiments from a change buried in the re-bootstrap batch', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emit({type: 'event_batch', events: preAttach, history_after_sequence: 0});
    const before = transport.requests.length;

    transport.emit({
      type: 'event_batch',
      events: [...rebootstrap, event(7, 'experiments_changed')],
      history_after_sequence: 4,
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(controller.state.core.historyAfterSequence).toBe(4);
    expect(
      transport.requests.slice(before).filter(request => request.type === 'query.experiments'),
    ).toHaveLength(1);
  });
});

/**
 * The same late attach, but the run log is shorter than the subscription's
 * tail, so the re-bootstrap replays it whole and declares floor 0 like the
 * batch before it. Nothing about the floors distinguishes the two logs; only
 * the store they name does.
 */
describe('a stream that re-bootstraps into a log shorter than the tail', () => {
  const runLog: RunEvent[] = [
    {
      ...event(1, 'run_started'),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    event(2, 'agent_output_chunk', 'two\n'),
    roundFinished(3, 1),
    event(4, 'agent_output_chunk', 'four\n'),
  ];
  // What the client folded from the server's own log before the attach: the
  // same sequence numbers, different events.
  const preAttach = [
    event(1, 'agent_output_chunk', 'server started\n'),
    event(2, 'agent_output_chunk', 'server ready\n'),
  ];

  async function rebootstrapped(transport: HistoryTransport): Promise<SocketSessionController> {
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.emitBatch(preAttach, 0, 'bootstrap-store');
    transport.emitBatch(runLog, 0, 'run-store');
    return controller;
  }

  it('reaches the state a full replay of the run log would have built', async () => {
    const replayedTransport = new HistoryTransport(runLog);
    const replayed = new SocketSessionController(replayedTransport);
    await replayed.start();
    replayedTransport.emitBatch(runLog, 0, 'run-store');

    const controller = await rebootstrapped(new HistoryTransport(runLog));

    expect(controller.state.core.transcript).toEqual(replayed.state.core.transcript);
    expect(controller.state.core.rounds).toEqual(replayed.state.core.rounds);
    expect(controller.state.core.sequence).toBe(replayed.state.core.sequence);
  });

  it('folds the run log prefix its stale cursor covered', async () => {
    const controller = await rebootstrapped(new HistoryTransport(runLog));

    // `run_started` is sequence 1 in the attached log and sequence 1 was
    // already folded from the log it replaces, so the out-of-order guard drops
    // it unless the batch is recognized as superseding what came before.
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.outerLoop).toBe('agent');
    expect(controller.state.core.rounds.map(round => round.number)).toEqual([1]);
  });

  it('declares a complete history, so nothing is left to backfill', async () => {
    const transport = new HistoryTransport(runLog);

    const controller = await rebootstrapped(transport);

    expect(controller.state.core.historyAfterSequence).toBe(0);
    await expect(controller.loadOlderHistory()).resolves.toBe(false);
    expect(eventsQueries(transport)).toEqual([]);
  });

  it('extends rather than re-folds while the store stays the same', async () => {
    const transport = new HistoryTransport(runLog);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitBatch(runLog.slice(0, 2), 0, 'run-store');
    transport.emitBatch(runLog.slice(2), 0, 'run-store');

    // A fresh run attaches its (empty) log without renumbering anything, so
    // its stream keeps one identity and the client must not discard the
    // events it already folded under it.
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.rounds.map(round => round.number)).toEqual([1]);
    expect(controller.state.core.transcript.map(item => item.content).join('')).toContain('two\n');
  });
});

describe('stream reconnect', () => {
  /** Lets the zero-delay reconnect timer and its subscribe settle. */
  const settle = () => new Promise<void>(resolve => setTimeout(resolve, 1));

  // The reconnect loop itself lives in and is tested against
  // PersistentEventStream; this checks only that the controller honors the
  // resumed tag it hands back, since the history floor is the controller's to
  // keep.
  it('keeps the history floor a resumed stream cannot vouch for', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    // A tail bootstrap: everything at or below sequence 5 is unread history.
    transport.emitBatch([event(6, 'agent_output_chunk', 'six\n')], 5);
    expect(controller.state.core.historyAfterSequence).toBe(5);

    transport.sever();
    await settle();
    // The resumed stream declares no floor of its own; taking its 0 literally
    // would claim the unread history below 5 is already loaded.
    transport.emitBatch([event(7, 'agent_output_chunk', 'seven\n')], 0);

    expect(controller.state.core.sequence).toBe(7);
    expect(controller.state.core.historyAfterSequence).toBe(5);
  });
});

class FakeTransport implements ServerTransport {
  closed = false;
  /** Mutable so a test can change what a refetch returns mid-run. */
  experiments: NonNullable<ProtocolResponse['experiments']> = [];
  experimentsReady = true;
  design: NonNullable<ProtocolResponse['design']> = [];
  designReady = true;
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
      experiments: this.experiments,
      experiments_ready: this.experimentsReady,
      design: this.design,
      design_ready: this.designReady,
      ...(input.type === 'query.chat'
        ? {
            chat: this.responseChat ?? {
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

/**
 * A backend whose stream a test can sever and whose dials it can refuse:
 * everything the reconnect path needs in order to be observed.
 */
class ReconnectTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  /** Every subscribe: the cursor and tail it carried, in order. */
  readonly subscribeCalls: Array<{afterSequence: number; tail: number | undefined}> = [];
  /** How many upcoming subscribes to reject before letting one through. */
  refuseSubscribes = 0;
  #message: ((message: ServerMessage) => void) | null = null;
  #disconnect: ((error: Error) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    return Promise.resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      ...(input.type === 'query.experiments' ? {experiments: [], experiments_ready: true} : {}),
    });
  }

  subscribe(
    afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription> {
    this.subscribeCalls.push({afterSequence, tail: options?.tail});
    if (this.refuseSubscribes > 0) {
      this.refuseSubscribes -= 1;
      return Promise.reject(new Error('connection refused'));
    }
    this.#message = onMessage;
    this.#disconnect = onDisconnect;
    return Promise.resolve({close: async () => undefined});
  }

  emitBatch(events: readonly RunEvent[], historyAfterSequence = 0): void {
    this.#message?.({
      type: 'event_batch',
      events: [...events],
      history_after_sequence: historyAfterSequence,
    });
  }

  sever(message = 'Server event stream disconnected'): void {
    this.#disconnect?.(new Error(message));
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

class DeferredExperimentsTransport implements ServerTransport {
  experimentRequests = 0;
  readonly #pending: Array<(response: ProtocolResponse) => void> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  request(input: RequestInput): Promise<ProtocolResponse> {
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({
        ...base,
        snapshot: {run_id: 'run', status: 'running', sequence: 0},
      });
    }
    if (input.type !== 'query.experiments') return Promise.resolve(base);
    this.experimentRequests += 1;
    if (this.experimentRequests === 1) {
      return Promise.resolve({...base, experiments: [], experiments_ready: true});
    }
    return new Promise(resolve => this.#pending.push(resolve));
  }

  resolveExperiment(experiments: NonNullable<ProtocolResponse['experiments']>): void {
    const resolve = this.#pending.shift();
    if (!resolve) throw new Error('No pending experiment request');
    resolve({
      protocol_version: 1,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
      experiments,
      experiments_ready: true,
    });
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
  ): Promise<EventSubscription> {
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  emit(runEvent: RunEvent): void {
    this.#message?.({type: 'event', event: runEvent});
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

class DeferredChatTransport implements ServerTransport {
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

/** A backend that creates one thread and answers threaded chat. */
class ThreadTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  #sequence = 0;
  #threads = 0;
  /** Providers and models the backend says this run offers. */
  chatOptions: NonNullable<ProtocolResponse['chat_options']> | null = {
    providers: [
      {
        provider: 'codex',
        models: [
          {model: 'gpt-run', source: 'run', default: true},
          {model: 'gpt-5.6-sol', source: 'suggested', default: false},
        ],
      },
      {provider: 'claude', models: [{model: 'claude-opus-5', source: 'suggested', default: false}]},
    ],
  };

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true,
    };
    if (input.type === 'query.snapshot') {
      return Promise.resolve({...base, snapshot: {run_id: 'run', status: 'running', sequence: 0}});
    }
    if (input.type === 'query.experiments') {
      return Promise.resolve({...base, experiments: [], experiments_ready: true});
    }
    if (input.type === 'query.chat_options') {
      return Promise.resolve({...base, chat_options: this.chatOptions});
    }
    if (input.type === 'query.chat_thread_create') {
      const threadId = `thread-${++this.#threads}`;
      // The backend resolves the run's own driver; the client never sends one.
      const settings = {
        driver: 'agentshim',
        provider: input.provider ?? 'codex',
        model: input.model ?? 'gpt-run',
      };
      return Promise.resolve({
        ...base,
        chat_thread: {thread_id: threadId, title: '', ...settings},
        events: [
          {
            sequence: ++this.#sequence,
            timestamp: '2026-01-01T00:00:01Z',
            type: 'chat_thread_created' as const,
            agent_kind: 'chat',
            round_label: 'experiment-chat',
            chat_thread_id: threadId,
            data: {
              kind: 'chat_thread_created' as const,
              thread_id: threadId,
              title: '',
              ...settings,
              created_at: '2026-01-01T00:00:01Z',
            },
          },
        ],
      });
    }
    if (input.type === 'query.chat') {
      const threadId = input.thread_id ?? null;
      return Promise.resolve({
        ...base,
        chat: {question: input.text, answer: 'Thread answer.', effect: 'none' as const},
        events: [
          {
            sequence: ++this.#sequence,
            timestamp: '2026-01-01T00:00:02Z',
            type: 'chat' as const,
            agent_kind: 'chat',
            round_label: 'experiment-chat',
            text: input.text,
            ...(threadId === null ? {} : {chat_thread_id: threadId}),
            data: {kind: 'chat' as const, answer: 'Thread answer.'},
          },
        ],
      });
    }
    return Promise.resolve(base);
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

/**
 * A tail-capable backend holding one run's whole history. The subscription
 * delivers whatever a test emits, and `query.events` answers out of the same
 * history, so a backfill returns exactly the range the controller asked for.
 */
class HistoryTransport implements ServerTransport {
  readonly requests: RequestInput[] = [];
  /** The `tail` each subscribe carried, in order; `undefined` for a plain one. */
  readonly subscribeTails: Array<number | undefined> = [];
  /** Rejects a subscribe carrying `tail`, the way a server without the field does. */
  rejectTail = false;
  /** Fails every subscribe, tail or not. */
  subscribeError: Error | null = null;
  /** Fails `query.events` instead of answering it. */
  eventsError: Error | null = null;
  /** Holds `query.events` answers until the test releases them. */
  deferEvents = false;
  readonly #pendingEvents: Array<() => void> = [];
  #message: ((message: ServerMessage) => void) | null = null;

  constructor(private readonly history: readonly RunEvent[] = []) {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    this.requests.push(input);
    const base = {
      protocol_version: 1 as const,
      request_id: 'request',
      timestamp: '2026-01-01T00:00:00Z',
      ok: true as const,
    };
    if (input.type !== 'query.events') return Promise.resolve(base);
    if (this.eventsError !== null) return Promise.reject(this.eventsError);
    const after = input.after_sequence ?? 0;
    const before = input.before_sequence ?? Number.MAX_SAFE_INTEGER;
    const response = {
      ...base,
      events: this.history.filter(
        item => (item.sequence ?? 0) > after && (item.sequence ?? 0) < before,
      ),
    };
    if (!this.deferEvents) return Promise.resolve(response);
    return new Promise(resolve => this.#pendingEvents.push(() => resolve(response)));
  }

  subscribe(
    _afterSequence: number,
    onMessage: (message: ServerMessage) => void,
    _onDisconnect: (error: Error) => void,
    options?: SubscribeOptions,
  ): Promise<EventSubscription> {
    this.subscribeTails.push(options?.tail);
    if (this.subscribeError !== null) return Promise.reject(this.subscribeError);
    if (this.rejectTail && options?.tail !== undefined) {
      return Promise.reject(new ServerError('Extra inputs are not permitted: tail'));
    }
    this.#message = onMessage;
    return Promise.resolve({close: async () => undefined});
  }

  /** One bootstrap batch: the spine below the floor, then the tail. */
  emitBatch(events: readonly RunEvent[], historyAfterSequence: number, storeId?: string): void {
    this.#message?.({
      type: 'event_batch',
      events: [...events],
      history_after_sequence: historyAfterSequence,
      // Omitted rather than empty when a test does not care, so the default
      // path stays what a server that reports no store identity sends.
      ...(storeId === undefined ? {} : {store_id: storeId}),
    });
  }

  releaseEvents(): void {
    const pending = this.#pendingEvents.shift();
    if (!pending) throw new Error('No pending query.events request');
    pending();
  }

  close(): Promise<void> {
    return Promise.resolve();
  }
}

function eventsQueries(transport: HistoryTransport): RequestInput[] {
  return transport.requests.filter(request => request.type === 'query.events');
}

function longHistory(length: number): RunEvent[] {
  return Array.from({length}, (_, index) =>
    event(index + 1, 'agent_output_chunk', `event ${index + 1}\n`),
  );
}

function roundFinished(sequence: number, round: number): RunEvent {
  return {
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'round_finished',
    round_label: `round-${round}`,
    data: {kind: 'round_finished', attempts: 1, judge_verdict: 'pass'},
  };
}

function perfRequests(transport: FakeTransport): number {
  return transport.requests.filter(request => request.type === 'query.performance').length;
}

function entry(
  id: string,
  firstRound: number,
  lastRound: number,
  overrides: Partial<NonNullable<ProtocolResponse['experiments']>[number]> = {},
): NonNullable<ProtocolResponse['experiments']>[number] {
  return {
    hypothesis_id: id,
    identified: true,
    first_round: firstRound,
    last_round: lastRound,
    rounds: [],
    kept: false,
    active: false,
    ...overrides,
  };
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
