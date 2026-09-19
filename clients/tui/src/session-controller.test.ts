import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {describe, expect, it} from 'bun:test';
import {
  type EventSubscription,
  type ProtocolResponse,
  AgentOutputChannel,
  type ChatResult,
  ChatModelSource,
  ChatOptionsSchema,
  type DesignRound,
  EventType,
  type ExperimentUpdateSchema,
  ExperimentsChangeReason,
  type HypothesisEntry,
  HypothesisEntrySchema,
  type PerformanceRound,
  PROTOCOL_VERSION,
  type ProtocolResponse,
  type RequestBody,
  ResponseSchema,
  type RunEvent,
  RunEventSchema,
  RunStatus,
  RoundJudgeVerdict,
  ServerError,
  type ServerMessage,
  ServerMessageSchema,
  type SubscribeOptions,
} from '@vibesys/backend-client';
import {makeEvent, makeEventBatch, makeSnapshot, timestampOf} from '@vibesys/backend-client/testing';
import {resolveStartupTrace} from './boot-trace.js';
import {fuzzyMatchCommands} from './commands.js';
import {type ServerTransport, SocketSessionController} from './session-controller.js';
import {chatPaneVisible, experimentLogVisible} from './session-model.js';

/** The command-bar palette's current matches, by name, for asserting on what `/help` offers. */
function paletteNames(controller: SocketSessionController): string[] {
  const context = {surface: 'command' as const, chatDocked: chatPaneVisible(controller.state)};
  return fuzzyMatchCommands('', context).map(command => command.name);
}

/** The chat palette's current matches, by name, for the chat-surface counterpart above. */
function chatPaletteNames(controller: SocketSessionController): string[] {
  const context = {surface: 'chat' as const, chatDocked: chatPaneVisible(controller.state)};
  return fuzzyMatchCommands('', context).map(command => command.name);
}

describe('session controller', () => {
  it('opens the palette locally without sending a backend command', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/help');

    expect(controller.state.palette).not.toBeNull();
    expect(paletteNames(controller)).toContain('/open-round');
    expect(transport.requests).toEqual([]);
  });

  it('opens the palette scoped to the chat surface from /help in the chat composer', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitChat('/help');

    expect(controller.state.palette).not.toBeNull();
    // The chat surface leads with its own thread commands, which the command
    // bar does not register at all.
    expect(chatPaletteNames(controller)).toContain('/switch');
    expect(transport.requests).toEqual([]);
  });

  it('keeps ordinary text out of commands and accepts it from Experiment chat', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('what is happening?');

    expect(transport.requests).toEqual([]);
    expect(controller.state.chatConversation).toEqual([]);
    // Ordinary text is a routing mistake, not a malformed command: it lands on
    // the command input's own hint row rather than raising the shared banner
    // (#564/#635's reasoning, applied to a wrong command instead of an empty
    // one).
    expect(controller.state.inputError).toContain('Not a command:');
    expect(controller.state.errorBanner).toBeNull();

    controller.clearInputError();
    await controller.submitChat('what is happening?');

    expect(transport.requests).toEqual([{case: 'chat', value: {text: 'what is happening?'}}]);
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

    transport.emit(makeEventBatch([event(1, EventType.AGENT_OUTPUT_CHUNK, 'one\n')]));
    transport.emit(eventMessage(event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n')));

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
        started.push(input.case);
        return new Promise(resolve => {
          pending.push(() =>
            resolve(respond()),
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
    expect([...started].sort()).toEqual(['experiments', 'snapshot', 'subscribe']);
    for (const resolve of pending.splice(0)) resolve();

    // The design log deliberately rides behind the experiments answer rather
    // than the boot barrier, so it is the one follow-up request here.
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(started).toContain('design');
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
          executionId: 'stale-execution',
          agentKind: 'implementer',
          roundLabel: 'round-1-implementer',
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
        event(2, EventType.AGENT_OUTPUT_CHUNK, 'persisted output\n'),
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
          ...event(1, EventType.RUN_FAILED),
          diagnostic: {
            code: 'interrupted',
            summary: 'A previous process was interrupted.',
            scope: 'run',
            severity: 'fatal',
            retryability: 'never',
          },
        },
        {
          ...event(2, EventType.RUN_STARTED),
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
          ...event(1, EventType.RUN_FAILED),
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
    transport.emit(eventMessage(event(1, EventType.RUN_FINISHED)));
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
        executionId: 'impl-1',
        agentKind: 'implementer',
        roundLabel: 'round-1-implementer',
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
          perfMetric: 1200,
          perfUnit: 'total_ops_per_sec',
          passed: true,
          profileSkipped: false,
        },
        {
          round: 2,
          perfMetric: 2400,
          perfUnit: 'total_ops_per_sec',
          passed: true,
          profileSkipped: false,
        },
      ],
    );
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/perf');

    expect(transport.requests).toEqual([{case: 'performance', value: {}}]);
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
        chatEvent(1, EventType.AGENT_OUTPUT_CHUNK, {
          kind: 'agent_output_chunk',
          channel: 'analysis',
          content: 'Reading progress.md',
        }),
        chatEvent(2, EventType.TOOL_CALL, {
          kind: 'tool_call',
          tool: 'read_file',
          args: {path: 'progress.md'},
          status: null,
        }),
        chatEvent(3, EventType.CHAT, {
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

    expect(transport.requests).toEqual([{case: 'chat', value: {text: 'what changed?'}}]);
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
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
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

  it('offers /chat in the palette only where the chat is not already on screen', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submitCommand('/help');
    expect(paletteNames(controller)).not.toContain('/chat');

    // Inside a hypothesis the chat is a dialog again, so the command returns.
    controller.enterExperimentDrilldown();
    await controller.submitCommand('/help');
    expect(paletteNames(controller)).toContain('/chat');
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
    expect(transport.requests).toEqual([{case: 'chat', value: {text: 'why?'}}]);
  });

  it('batches messages queued while the chat agent is still working', async () => {
    const transport = new DeferredChatTransport();
    const controller = new SocketSessionController(transport);

    const first = controller.sendChat('first question');
    const second = controller.sendChat('follow-up question');
    const third = controller.sendChat('one more detail');

    expect(transport.requests).toEqual([{case: 'chat', value: {text: 'first question'}}]);
    expect(controller.state.chatConversation).toMatchObject([
      {kind: 'user', label: 'You', content: 'first question'},
      {kind: 'user', label: 'You · queued', content: 'follow-up question'},
      {kind: 'user', label: 'You · queued', content: 'one more detail'},
    ]);

    transport.resolveNext('first answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests).toEqual([
      {case: 'chat', value: {text: 'first question'}},
      {case: 'chat', value: {text: 'follow-up question\n\none more detail'}},
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

    expect(transport.requests.at(-1)).toEqual({case: 'chat', value: {text: 'second\n\nthird'}});

    const fourth = controller.sendChat('fourth');
    transport.resolveNext('batched answer');
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.at(-1)).toEqual({case: 'chat', value: {text: 'fourth'}});

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
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    await controller.start();

    expect(transport.requests).toContainEqual({case: 'experiments', value: {}});
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
    expect(controller.state.overlay).toBeNull();
  });

  it('rejects the removed experiment-log commands without reaching the backend', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/history');
    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.inputError).toContain('Unknown command /history');

    await controller.submitCommand('/history rounds');
    expect(controller.state.inputError).toContain('Unknown command /history rounds');

    await controller.submitCommand('/experiments');
    expect(controller.state.inputError).toContain('Unknown command /experiments');

    controller.clearInputError();
    expect(controller.state.inputError).toBeNull();

    await controller.submitCommand('/history');
    expect(controller.state.inputError).toContain('Unknown command /history');

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
      entry('H-01', 1, 1, {resolvedOutcome: 'proven'}),
      entry('H-02', 2, 2, {active: true}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    controller.moveExperimentSelection(1);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
    const before = transport.requests.length;

    // The active hypothesis resolves and a new one opens above nothing.
    transport.experiments = [
      entry('H-01', 1, 1, {resolvedOutcome: 'proven'}),
      entry('H-02', 2, 3, {resolvedOutcome: 'rejected'}),
      entry('H-03', 4, 4, {active: true}),
    ];
    transport.emit(eventMessage(event(9, EventType.EXPERIMENTS_CHANGED)));
    await Promise.resolve();
    await Promise.resolve();

    const refetches = transport.requests.slice(before).filter(r => r.case === 'experiments');
    expect(refetches).toHaveLength(1);
    expect(controller.state.experimentLog?.entries).toHaveLength(3);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
    expect(controller.state.experimentLog?.entries[1]?.resolvedOutcome).toBe('rejected');
  });

  it('applies a revisioned replacement without dropping unchanged hypotheses', async () => {
    const transport = new RevisionedExperimentsTransport([
      entry('H-01', 1, 1, {resolvedOutcome: 'proven'}),
      entry('H-02', 2, 2, {active: true}),
    ]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    expect(transport.experimentInputs.at(-1)).toEqual({
      case: 'experiments',
      value: {after: {runId: 'run', projectionId: 'projection', revision: 1}},
    });
    transport.resolveExperiment([entry('H-02', 2, 3, {resolvedOutcome: 'rejected'})], {
      runId: 'run',
      projectionId: 'projection',
      fromRevision: 1,
      throughRevision: 2,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries.map(item => item.hypothesisId)).toEqual([
      'H-01',
      'H-02',
    ]);
    expect(controller.state.experimentLog?.entries[1]?.resolvedOutcome).toBe('rejected');
  });

  it('recovers from a delta whose base does not match the applied cursor', async () => {
    const transport = new RevisionedExperimentsTransport([entry('H-old', 1, 1, {})]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    transport.resolveExperiment([entry('H-wrong', 2, 2, {})], {
      runId: 'run',
      projectionId: 'projection',
      fromRevision: 0,
      throughRevision: 2,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentInputs.at(-1)).toEqual({case: 'experiments', value: {}});
    transport.resolveExperiment([entry('H-current', 1, 2, {active: true})], {
      runId: 'run',
      projectionId: 'projection',
      throughRevision: 2,
      reset: true,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries.map(item => item.hypothesisId)).toEqual([
      'H-current',
    ]);
  });

  it('converges through a burst that advances while a delta is in flight', async () => {
    const transport = new RevisionedExperimentsTransport([entry('H-01', 1, 1, {})]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    transport.emitExperimentChange(3, 3);
    transport.resolveExperiment([entry('H-01', 1, 2, {})], {
      runId: 'run',
      projectionId: 'projection',
      fromRevision: 1,
      throughRevision: 2,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentInputs.at(-1)).toEqual({
      case: 'experiments',
      value: {after: {runId: 'run', projectionId: 'projection', revision: 2}},
    });
    transport.resolveExperiment([entry('H-01', 1, 3, {resolvedOutcome: 'proven'})], {
      runId: 'run',
      projectionId: 'projection',
      fromRevision: 2,
      throughRevision: 3,
      reset: false,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries[0]?.last_round).toBe(3);
    expect(transport.experimentInputs).toHaveLength(3);
  });

  it('rejects an in-flight response when the same run id is attached from another project', async () => {
    const transport = new RevisionedExperimentsTransport([entry('H-old', 1, 1, {})]);
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emitExperimentChange(2, 2);
    transport.emitProjectAttached(3);
    transport.resolveExperiment([entry('H-stale', 1, 2, {})], {
      runId: 'run',
      projectionId: 'old-project',
      throughRevision: 2,
      reset: true,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries[0]?.hypothesisId).toBe('H-old');
    expect(transport.experimentInputs).toHaveLength(3);
    transport.resolveExperiment([entry('H-new', 1, 1, {active: true})], {
      runId: 'run',
      projectionId: 'new-project',
      throughRevision: 1,
      reset: true,
    });
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.experimentLog?.entries[0]?.hypothesisId).toBe('H-new');
  });

  it('does not refetch the log for events that cannot change it', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    transport.emit(eventMessage(event(1, EventType.AGENT_OUTPUT_CHUNK, 'noise\n')));
    transport.emit(eventMessage(event(2, EventType.TOOL_CALL)));
    transport.emit(eventMessage(event(3, EventType.PHASE_FINISHED)));
    transport.emit(eventMessage(event(4, EventType.ROUND_FINISHED)));
    await Promise.resolve();

    expect(transport.requests).toHaveLength(before);
  });

  it('keeps the log as the root view, with no way to dismiss it', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 2, {
        resolvedOutcome: 'proven',
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
        resolvedOutcome: 'proven',
        rounds: [{round: 1, passed: true, reviewed: true}],
      }),
      entry('H-02', 2, 3, {
        resolvedOutcome: 'rejected',
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
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    expect(controller.state.experimentLog?.pending).toBe(true);
    await controller.start();

    expect(transport.requests).toEqual([
      {case: 'snapshot', value: {}},
      {case: 'experiments', value: {}},
      {case: 'design', value: {}},
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

  it('opens the newest diffable round and fetches only the file on screen', async () => {
    const transport = new FakeTransport();
    transport.design = [
      {
        round: 1,
        base: 'aaa1111',
        commit: 'bbb2222',
        files: [
          {path: 'src/ring.rs', change: 'modified'},
          {path: 'src/lib.rs', change: 'modified'},
        ],
      },
      // Newer but not diffable: the opener walks back to round 1.
      {round: 2, base: 'bbb2222', commit: 'ccc3333', files: []},
    ];
    transport.designPatchText = '@@ -1 +1 @@\n-old\n+new\n';
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    controller.openRoundDiff();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.diffViewer).toMatchObject({
      round: 1,
      base: 'aaa1111',
      head: 'bbb2222',
      index: 0,
    });
    // Lazy per file: opening asked for the visible file only.
    expect(patchRequests(transport)).toEqual([
      {case: 'designPatch', value: {base: 'aaa1111', head: 'bbb2222', path: 'src/ring.rs'}},
    ]);
    expect(controller.state.diffViewer?.patches['src/ring.rs']).toEqual({
      kind: 'loaded',
      patch: '@@ -1 +1 @@\n-old\n+new\n',
      truncated: false,
    });

    // The next file costs one query; returning to a visited file costs none.
    controller.moveDiffFile(1);
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    controller.moveDiffFile(-1);
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(patchRequests(transport)).toHaveLength(2);
    expect(patchRequests(transport).at(-1)).toMatchObject({path: 'src/lib.rs'});
  });

  it("diffs the drill-down's selected round rather than the newest one", async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 2, {
        rounds: [
          {round: 1, passed: true, reviewed: true},
          {round: 2, passed: true, reviewed: true},
        ],
      }),
    ];
    transport.design = [
      {round: 1, base: 'aaa1111', commit: 'bbb2222', files: [{path: 'src/a.rs', change: 'added'}]},
      {round: 2, base: 'bbb2222', commit: 'ccc3333', files: [{path: 'src/b.rs', change: 'added'}]},
    ];
    transport.designPatchText = '@@ -0,0 +1 @@\n+fn main() {}\n';
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    controller.enterExperimentDrilldown();
    controller.moveHypothesisRoundSelection(-1);
    expect(controller.state.hypothesisDetail?.selectedRound).toBe(1);

    controller.openRoundDiff();
    expect(controller.state.diffViewer).toMatchObject({round: 1, base: 'aaa1111'});
    // The viewer replaced no overlay here, and closing restores the detail
    // view untouched underneath.
    controller.closeDiffViewer();
    expect(controller.state.diffViewer).toBeNull();
    expect(controller.state.hypothesisDetail?.selectedRound).toBe(1);
  });

  it('parks a failed patch query on the file, never the shared banner', async () => {
    const transport = new FakeTransport();
    transport.design = [
      {round: 1, base: 'aaa1111', commit: 'bbb2222', files: [{path: 'src/a.rs', change: 'added'}]},
    ];
    transport.designPatchError = new ServerError('base does not name a commit');
    const controller = new SocketSessionController(transport);
    await controller.start();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    controller.openRoundDiff();
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.diffViewer?.patches['src/a.rs']).toEqual({
      kind: 'error',
      message: 'base does not name a commit',
    });
    expect(controller.state.errorBanner).toBeNull();
  });

  it('explains an undiffable request in the detail overlay instead of opening', async () => {
    const transport = new FakeTransport();
    transport.designReady = false;
    const controller = new SocketSessionController(transport);
    await controller.start();

    controller.openRoundDiff();
    expect(controller.state.diffViewer).toBeNull();
    expect(controller.state.overlay?.content).toContain('No design rounds have loaded yet');

    // With a log whose rounds recorded no changes, the wording is per round.
    transport.design = [{round: 1, base: 'aaa1111', commit: 'bbb2222', files: []}];
    transport.designReady = true;
    await controller.submitCommand('/design');
    controller.openRoundDiff(1);
    expect(controller.state.diffViewer).toBeNull();
    expect(controller.state.overlay?.content).toBe('Round 1 changed no workspace files.');
    controller.openRoundDiff(9);
    expect(controller.state.overlay?.content).toBe('Round 9 has no recorded design changes.');
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
    transport.emit(eventMessage(event(1, EventType.EXPERIMENTS_CHANGED)));
    await Promise.resolve();
    await Promise.resolve();

    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.selectedId).toBe('H-resumed');
  });

  it('reports how long the landing view waited for experiments', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
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
    transport.emit(eventMessage(event(1, EventType.EXPERIMENTS_CHANGED)));
    await Promise.resolve();
    await Promise.resolve();

    expect(traced).toEqual([expect.stringMatching(/^experiments loaded in \d+ms \(2 entries\)$/)]);

    // A later refresh is not a boot cost, so it does not report again.
    transport.emit(eventMessage(event(2, EventType.EXPERIMENTS_CHANGED)));
    await Promise.resolve();
    await Promise.resolve();

    expect(traced).toHaveLength(1);
  });

  it('stays silent through the real sink unless the boot trace is switched on', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
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
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
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
    transport.experiments = [entry('H-01', 1, 1, {resolvedOutcome: 'proven'})];
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
      events: [event(1, EventType.EXPERIMENTS_CHANGED), event(2, EventType.EXPERIMENTS_CHANGED)],
    });
    await Promise.resolve();
    await Promise.resolve();

    const refetches = transport.requests.slice(before).filter(r => r.case === 'experiments');
    expect(refetches).toHaveLength(1);
  });

  it('refetches again when experiments change during an in-flight fetch', async () => {
    const transport = new DeferredExperimentsTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();

    transport.emit(event(1, EventType.EXPERIMENTS_CHANGED));
    expect(transport.experimentRequests).toBe(2);

    transport.emit(event(2, EventType.EXPERIMENTS_CHANGED));
    transport.emit(event(3, EventType.EXPERIMENTS_CHANGED));
    transport.resolveExperiment([entry('H-stale', 1, 1, {active: true})]);
    // A macrotask, because the design refresh sits between the answer and the
    // queued refetch and its length is not this test's concern.
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.experimentRequests).toBe(3);
    transport.resolveExperiment([entry('H-current', 1, 2, {resolvedOutcome: 'proven'})]);
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
      event: {...event(1, EventType.PHASE_STARTED), agentKind: 'orchestrator', roundLabel: 'round-9-pre'},
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
      event: {...event(1, EventType.PHASE_STARTED), agentKind: 'orchestrator', roundLabel: 'round-1-pre'},
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
      [{round: 1, perfMetric: 1200, perfUnit: 'ops', passed: true, profileSkipped: false}],
    );
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submitCommand('/perf');
    const before = perfRequests(transport);

    transport.emit(eventMessage(event(9, EventType.ROUND_FINISHED)));
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

    transport.emit(eventMessage(event(9, EventType.ROUND_FINISHED)));
    await Promise.resolve();

    expect(perfRequests(transport)).toBe(before);
    expect(controller.state.layout.right).toBeNull();
  });

  it('fetches the view opened while another view’s query is in flight', async () => {
    const transport = new DeferredDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.deferDesign = true;

    const designOpen = controller.openPane('design');
    const perfOpen = controller.openPane('perf');
    // The perf pane is on screen and waiting; its query must not have been
    // swallowed by the design query still in flight.
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.pending).toBe(true);
    expect(transport.requests.filter(request => request.case === 'performance')).toHaveLength(
      0,
    );

    transport.releaseDesign();
    await Promise.all([designOpen, perfOpen]);
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(transport.requests.filter(request => request.case === 'performance')).toHaveLength(
      1,
    );
    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.pending).toBe(false);
    expect(controller.state.layout.right?.content).toContain('Performance · total_ops_per_sec');
  });

  it('drops the superseded design answer rather than painting it over the perf pane', async () => {
    const transport = new DeferredDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.deferDesign = true;

    const designOpen = controller.openPane('design');
    const perfOpen = controller.openPane('perf');
    transport.releaseDesign();
    await Promise.all([designOpen, perfOpen]);
    await new Promise<void>(resolve => setTimeout(resolve, 0));

    expect(controller.state.layout.right?.view).toBe('perf');
    expect(controller.state.layout.right?.content).toContain('Performance · total_ops_per_sec');
    expect(controller.state.layout.right?.content).not.toContain('Design changes by round');
  });

  it('coalesces same-view refreshes during a fetch into one follow-up', async () => {
    const transport = new DeferredDesignTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    transport.deferDesign = true;

    const designOpen = controller.openPane('design');
    // Two rounds finish while the query is still running. The in-flight
    // answer predates both, so one refetch follows, not one per event.
    transport.emit(event(9, EventType.ROUND_FINISHED));
    transport.emit(event(10, EventType.ROUND_FINISHED));
    const before = designQueries(transport);

    transport.releaseDesign();
    await designOpen;
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(designQueries(transport) - before).toBe(1);

    transport.releaseDesign();
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    expect(controller.state.layout.right?.pending).toBe(false);
    expect(designQueries(transport) - before).toBe(1);
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
      [{round: 1, perfMetric: 1200, perfUnit: 'ops', passed: true, profileSkipped: false}],
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
    expect(transport.requests.slice(before)).toEqual([{case: 'performance', value: {}}]);
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

    expect(transport.requests.at(-1)).toEqual({case: 'chatOptions', value: {}});
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

    expect(transport.requests.at(-1)).toEqual({case: 'chatOptions', value: {}});
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

    expect(transport.requests.at(-1)).toEqual({case: 'chatThreadCreate', value: {}});
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

    expect(transport.requests.at(-1)).toEqual({case: 'pause', value: {}});
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
      threadId: 'thread-1',
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
    expect(transport.requests).toContainEqual({case: 'resume', value: {}});
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

    // A usage error is `scope: 'input'` regardless of which surface typed it,
    // so it lands on the command input's hint rather than the banner.
    expect(controller.state.inputError).toBe('Usage: /clear');
    expect(controller.state.errorBanner).toBeNull();
    // The phrase started no thread and sent no request.
    expect(transport.requests.length).toBe(before);
    expect(controller.state.chatMenu).toBeNull();
  });

  it('does not run a no-argument command typed with a trailing phrase', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/pause typo');

    expect(controller.state.inputError).toBe('Usage: /pause');
    expect(controller.state.errorBanner).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('reports an unknown theme as an error without switching', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitCommand('/theme monokai');

    expect(controller.state.errorBanner).toBeNull();
    expect(controller.state.inputError).toContain('Unknown theme: monokai');
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
      event(1, EventType.AGENT_OUTPUT_CHUNK, 'one\n'),
      event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n'),
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
      {case: 'events', value: {afterSequence: 500, beforeSequence: 1_501}},
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
    transport.emitBatch([event(2_001, EventType.AGENT_OUTPUT_CHUNK, 'live\n')], 1_500);

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
    const tail = event(5, EventType.AGENT_OUTPUT_CHUNK, 'five\n');
    const history = [
      event(1, EventType.AGENT_OUTPUT_CHUNK, 'one\n'),
      firstRound,
      event(3, EventType.AGENT_OUTPUT_CHUNK, 'three\n'),
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
      ...event(1, EventType.RUN_STARTED),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n'),
    roundFinished(3, 1),
    event(4, EventType.AGENT_OUTPUT_CHUNK, 'four\n'),
    event(5, EventType.AGENT_OUTPUT_CHUNK, 'five\n'),
    event(6, EventType.AGENT_OUTPUT_CHUNK, 'server started\n'),
  ];
  /** What the stream sends once the run log is attached: spine, then tail. */
  const rebootstrap = [runLog[0], runLog[2], runLog[4], runLog[5]] as RunEvent[];
  const preAttach = [event(1, EventType.AGENT_OUTPUT_CHUNK, 'server started\n')];

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
      {case: 'events', value: {afterSequence: 0, beforeSequence: 5}},
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
    transport.emit(makeEventBatch(preAttach, undefined, {historyAfterSequence: 0}));
    const before = transport.requests.length;

    transport.emit({
      type: 'event_batch',
      events: [...rebootstrap, event(7, EventType.EXPERIMENTS_CHANGED)],
      history_after_sequence: 4,
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(controller.state.core.historyAfterSequence).toBe(4);
    expect(
      transport.requests.slice(before).filter(request => request.case === 'experiments'),
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
      ...event(1, EventType.RUN_STARTED),
      data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
    },
    event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n'),
    roundFinished(3, 1),
    event(4, EventType.AGENT_OUTPUT_CHUNK, 'four\n'),
  ];
  // What the client folded from the server's own log before the attach: the
  // same sequence numbers, different events.
  const preAttach = [
    event(1, EventType.AGENT_OUTPUT_CHUNK, 'server started\n'),
    event(2, EventType.AGENT_OUTPUT_CHUNK, 'server ready\n'),
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
    const experimentRequests = transport.requests.filter(
      request => request.case === 'experiments',
    ).length;
    // A tail bootstrap: everything at or below sequence 5 is unread history.
    transport.emitBatch([event(6, EventType.AGENT_OUTPUT_CHUNK, 'six\n')], 5);
    expect(controller.state.core.historyAfterSequence).toBe(5);

    transport.sever();
    await settle();
    expect(transport.requests.filter(request => request.case === 'experiments')).toHaveLength(
      experimentRequests + 1,
    );
    // The resumed stream declares no floor of its own; taking its 0 literally
    // would claim the unread history below 5 is already loaded.
    transport.emitBatch([event(7, EventType.AGENT_OUTPUT_CHUNK, 'seven\n')], 0);

    expect(controller.state.core.sequence).toBe(7);
    expect(controller.state.core.historyAfterSequence).toBe(5);
  });

  it('re-bootstraps when the store was swapped while the stream was severed', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    // Boot against the bootstrap store: sequences 1 and 2 are folded from it.
    transport.emitBatch(
      [
        event(1, EventType.AGENT_OUTPUT_CHUNK, 'server started\n'),
        event(2, EventType.AGENT_OUTPUT_CHUNK, 'server ready\n'),
      ],
      0,
      'bootstrap-store',
    );
    expect(controller.state.core.sequence).toBe(2);

    transport.sever();
    await settle();
    // While severed, the durable log was attached: a new store whose sequence 1
    // is `run_started`, not the output the client folded at 1 in the old store.
    transport.emitBatch(
      [
        {
          ...event(1, EventType.RUN_STARTED),
          data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
        },
        event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n'),
      ],
      0,
      'run-store',
    );

    // The resume named the store the client last saw, and the batch that named
    // a different one superseded the fold: `run_started` at sequence 1 lands
    // even though sequence 1 was already folded, which the extend path drops.
    expect(transport.subscribeCalls.at(-1)?.storeId).toBe('bootstrap-store');
    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.outerLoop).toBe('agent');
    expect(controller.state.core.historyAfterSequence).toBe(0);
  });

  it('keeps the fold when an old-server fallback omits store identity', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0]);
    await controller.start();
    transport.emitBatch(
      [
        {
          ...event(1, EventType.RUN_STARTED),
          data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
        },
      ],
      0,
      'run-store',
    );

    // A server predating store-aware resumes rejects the first dial, then the
    // stream falls back to a plain cursor resume whose suffix names no store.
    transport.refuseSubscribes = 1;
    transport.sever();
    await settle();
    expect(transport.subscribeCalls.at(-1)?.storeId).toBeUndefined();
    transport.emitBatch([event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n')]);

    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.sequence).toBe(2);
    expect(controller.state.core.transcript.map(item => item.content).join('')).toContain('two\n');
  });

  it('keeps the fold and learns an identity first seen on a resumed suffix', async () => {
    const transport = new ReconnectTransport();
    const controller = new SocketSessionController(transport, undefined, undefined, [0, 0]);
    await controller.start();
    transport.emitBatch([
      {
        ...event(1, EventType.RUN_STARTED),
        data: {kind: 'run_started', outer_loop: 'agent', input: '.', max_rounds: 3},
      },
    ]);

    transport.sever();
    await settle();
    transport.emitBatch([event(2, EventType.AGENT_OUTPUT_CHUNK, 'two\n')], 0, 'run-store');

    expect(controller.state.core.maxRounds).toBe(3);
    expect(controller.state.core.sequence).toBe(2);
    expect(controller.state.core.transcript.map(item => item.content).join('')).toContain('two\n');

    // Once learned, the identity protects the next cursor resume.
    transport.sever();
    await settle();
    expect(transport.subscribeCalls.at(-1)?.storeId).toBe('run-store');
  });
});

const CHANNELS = [
  AgentOutputChannel.ASSISTANT,
  AgentOutputChannel.ASSISTANT,
  AgentOutputChannel.ASSISTANT,
  AgentOutputChannel.ANALYSIS,
  AgentOutputChannel.PROMPT,
] as const;

type EventInit = NonNullable<Parameters<typeof makeEvent>[1]>;

/**
 * A synthetic run log shaped like a real one: rounds of agent executions with
 * streamed output, paired tool calls, todo and usage updates, per-round judge
 * and benchmark results, and chat traffic across several threads.
 *
 * `typedTools` picks the producer flavor: typed `tool_call`/`tool_result`
 * events, or the legacy `tool`-channel chunks a driver without structured tool
 * reporting emits. One log carries one flavor, because the reducer's
 * typed-tool latch makes a log carrying both order-dependent (see the mixed
 * producer test).
 *
 * Sized well under MAX_TRANSCRIPT_ENTRIES so cap eviction, which replay and
 * backfill are not required to agree on, never enters the comparison.
 */

// biome-ignore lint/complexity/noExcessiveCognitiveComplexity: pre-existing; tracked: #288
function generateRunEvents(seed: number, options: {typedTools: boolean}, rounds = 5): RunEvent[] {
  const rng = new Rng(seed);
  const events: RunEvent[] = [];
  const threadIds = ['thread-a', 'thread-b', 'thread-c'];
  let clock = 0;

  const emit = (type: EventType, init: EventInit = {}): void => {
    clock += rng.int(5, 400);
    events.push(makeEvent(type, {...init, sequence: events.length + 1, timestamp: isoAt(clock)}));
  };

  emit(EventType.SERVER_STARTED, {status: EventStatus.ACTIVE});
  emit(EventType.SERVER_READY, {data: {case: 'serverReady', value: {}}});
  emit(EventType.RUN_STARTED, {
    status: EventStatus.ACTIVE,
    data: {
      case: 'runStarted',
      value: {
        outerLoop: 'agent',
        input: '/synthetic/target',
        maxRounds: rounds,
        expectedRoles: [...AGENT_KINDS],
      },
    },
  });

  for (let round = 1; round <= rounds; round += 1) {
    const roundLabel = `round-${round}`;
    for (const kind of AGENT_KINDS) {
      const executionId = `exec-${round}-${kind}`;
      const prompt = words(rng, 5, 20);
      const context = {agentKind: kind, roundLabel, executionId};
      emit(EventType.AGENT_EXECUTION_STARTED, {
        ...context,
        status: EventStatus.ACTIVE,
        data: {
          case: 'agentExecutionStarted',
          value: {
            stage: kind,
            systemPrompt: prompt,
            userPrompt: prompt,
            activity: {mode: ExecutionActivityMode.THINKING, summary: 'Thinking'},
            driver: 'agentshim',
            provider: 'anthropic',
            model: 'claude-sonnet',
          },
        },
      });
      emit(EventType.PHASE_STARTED, {
        ...context,
        status: EventStatus.ACTIVE,
        data: {case: 'phase', value: {phase: kind}},
      });
      emit(EventType.INVOCATION_STARTED, {
        ...context,
        status: EventStatus.ACTIVE,
        data: {case: 'invocationStarted', value: {systemPrompt: prompt, userPrompt: prompt}},
      });

      const turns = rng.int(4, 12);
      for (let turn = 0; turn < turns; turn += 1) {
        emit(EventType.AGENT_OUTPUT_CHUNK, {
          ...context,
          data: {
            case: 'agentOutputChunk',
            value: {channel: rng.pick(CHANNELS), content: words(rng, 3, 25)},
          },
        });
        if (rng.float() < 0.35) {
          const callId = `${executionId}-call-${turn}`;
          const tool = rng.pick(TOOLS);
          if (options.typedTools) {
            emit(EventType.TOOL_CALL, {
              ...context,
              data: {case: 'toolCall', value: {tool, callId, args: {pattern: words(rng, 2, 5)}}},
            });
            emit(EventType.TOOL_RESULT, {
              ...context,
              data: {
                case: 'toolResult',
                value: {
                  tool,
                  callId,
                  content: words(rng, 5, 40),
                  isError: rng.float() < 0.05,
                },
              },
            });
          } else {
            emit(EventType.AGENT_OUTPUT_CHUNK, {
              ...context,
              data: {
                case: 'agentOutputChunk',
                value: {
                  channel: AgentOutputChannel.TOOL,
                  content: `→ ${tool} ${words(rng, 2, 5)}`,
                },
              },
            });
            emit(EventType.AGENT_OUTPUT_CHUNK, {
              ...context,
              data: {
                case: 'agentOutputChunk',
                value: {channel: AgentOutputChannel.TOOL, content: words(rng, 5, 40)},
              },
            });
          }
        }
        if (rng.float() < 0.1) {
          emit(EventType.TODO_UPDATE, {
            ...context,
            data: {
              case: 'todoUpdate',
              value: {
                todos: ['completed', 'in_progress', 'pending'].map(status => ({
                  content: words(rng, 2, 6),
                  status,
                })),
              },
            },
          });
        }
        if (rng.float() < 0.15) {
          emit(EventType.USAGE_UPDATE, {
            ...context,
            data: {
              case: 'usageUpdate',
              value: {
                inputTokens: rng.int(2000, 180000),
                contextWindow: 200000,
                model: 'claude-sonnet',
              },
            },
          });
        }
      }

      emit(EventType.AGENT_EXECUTION_FINISHED, {
        ...context,
        status: EventStatus.COMPLETED,
        data: {case: 'agentExecutionFinished', value: {}},
      });
      emit(EventType.INVOCATION_FINISHED, {
        ...context,
        status: EventStatus.COMPLETED,
        data: {case: 'invocationFinished', value: {}},
      });
      emit(EventType.PHASE_FINISHED, {
        ...context,
        status: EventStatus.COMPLETED,
        data: {case: 'phase', value: {phase: kind}},
      });
    }

    emit(EventType.JUDGE_RESULT, {
      roundLabel,
      data: {
        case: 'judgeResult',
        value: {
          verdict: rng.float() < 0.7 ? JudgeVerdict.PASS : JudgeVerdict.FAIL,
          feedback: words(rng, 5, 20),
          attempt: 1,
        },
      },
    });
    emit(EventType.BENCHMARK_RESULT, {
      roundLabel,
      data: {
        case: 'benchmarkResult',
        value: {metric: 'throughput', value: rng.int(100000, 5000000), unit: 'ops/s'},
      },
    });
    emit(EventType.ROUND_FINISHED, {
      roundLabel,
      status: EventStatus.COMPLETED,
      data: {
        case: 'roundFinished',
        value: {
          attempts: 1,
          judgeVerdict: rng.float() < 0.7 ? RoundJudgeVerdict.PASS : RoundJudgeVerdict.FAIL,
          perfMetric: rng.int(100000, 5000000),
          perfUnit: 'ops/s',
        },
      },
    });
    if (rng.float() < 0.3) {
      emit(EventType.EXPERIMENTS_CHANGED, {
        data: {
          case: 'experimentsChanged',
          value: {reason: ExperimentsChangeReason.ROUND_PERSISTED},
        },
      });
    }
    if (rng.float() < 0.5) {
      // A thread the operator opens mid-run, titled only on a later turn, and
      // occasionally the implicit default thread with no id at all.
      const threadId = rng.float() < 0.2 ? undefined : rng.pick(threadIds);
      const chatContext = {
        agentKind: 'chat',
        roundLabel: 'experiment-chat',
        ...(threadId === undefined ? {} : {chatThreadId: threadId}),
      };
      if (threadId !== undefined) {
        emit(EventType.CHAT_THREAD_CREATED, {
          ...chatContext,
          data: {
            case: 'chatThreadCreated',
            value: {
              threadId,
              title: '',
              driver: 'agentshim',
              provider: 'anthropic',
              model: 'claude-sonnet',
              createdAt: isoAt(clock),
            },
          },
        });
      }
      // Most turns stream their answer before the terminal record. A streamed
      // turn is sometimes abandoned (its invocation failed before recording an
      // answer), and an unstreamed answer is sometimes a legacy id-less record
      // from before answers carried their invocation.
      const invocationId = `chat-${events.length}`;
      const streamed = rng.float() < 0.6;
      if (streamed) {
        for (let chunk = rng.int(1, 3); chunk > 0; chunk -= 1) {
          emit(EventType.AGENT_OUTPUT_CHUNK, {
            ...chatContext,
            executionId: invocationId,
            data: {
              case: 'agentOutputChunk',
              value: {channel: AgentOutputChannel.ASSISTANT, content: `${words(rng, 2, 6)} `},
            },
          });
        }
      }
      const abandoned = streamed && rng.float() < 0.25;
      if (!abandoned) {
        emit(EventType.CHAT, {
          ...chatContext,
          status: EventStatus.ANSWERED,
          data: {
            case: 'chat',
            value: {
              answer: words(rng, 5, 30),
              ...(rng.float() < 0.5 ? {threadTitle: words(rng, 2, 4)} : {}),
              ...(streamed || rng.float() < 0.5 ? {invocationId} : {}),
            },
          },
        });
      }
    }
  }

  emit(EventType.RUN_FINISHED, {status: EventStatus.COMPLETED});
  return events;
}

function words(rng: Rng, min: number, max: number): string {
  const count = rng.int(min, max);
  const chosen: string[] = [];
  for (let at = 0; at < count; at += 1) chosen.push(rng.pick(WORDS));
  return chosen.join(' ');
}

function isoAt(millis: number): Timestamp {
  return timestampOf(new Date(Date.UTC(2026, 7, 20) + millis));
}

/** The wire timestamp of event `sequence`. */
function timestamp(sequence: number): Timestamp {
  return timestampOf(`2026-01-01T00:00:0${sequence}Z`);
}

/** The ISO string core-state derives from `timestamp(sequence)`. */
function iso(sequence: number): string {
  return `2026-01-01T00:00:0${sequence}.000Z`;
}

function baseEvent(sequence: number, type: EventType, init: EventInit = {}): RunEvent {
  return makeEvent(type, {
    sequence,
    timestamp: timestamp(sequence),
    agentKind: 'implementer',
    roundLabel: 'round-1-implementer',
    ...init,
  });
}

function chunkEvent(sequence: number, content = `entry ${sequence}`): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    executionId: 'turn',
    data: {case: 'agentOutputChunk', value: {channel: AgentOutputChannel.ASSISTANT, content}},
  });
}

function executionStatusEvent(
  sequence: number,
  executionId: string,
  kind: 'agent_output_chunk' | 'tool_call',
  status: {
    progress?: string;
    agentLabel?: string;
    elapsedSeconds?: number;
    inputTokens?: number;
    contextWindow?: number;
  },
): RunEvent {
  return baseEvent(
    sequence,
    kind === 'agent_output_chunk' ? EventType.AGENT_OUTPUT_CHUNK : EventType.TOOL_CALL,
    {
      executionId,
      data:
        kind === 'agent_output_chunk'
          ? {
              case: 'agentOutputChunk',
              value: {
                channel: AgentOutputChannel.ANALYSIS,
                content: status.progress ?? '',
                status,
              },
            }
          : {case: 'toolCall', value: {tool: 'Bash', callId: `call-${sequence}`, args: {}, status}},
    },
  );
}

function toolCallEvent(sequence: number, callId: string): RunEvent {
  return baseEvent(sequence, EventType.TOOL_CALL, {
    executionId: 'turn',
    data: {case: 'toolCall', value: {tool: 'Bash', callId, args: {command: callId}}},
  });
}

function toolResultEvent(sequence: number, callId: string, content: string): RunEvent {
  return baseEvent(sequence, EventType.TOOL_RESULT, {
    executionId: 'turn',
    data: {case: 'toolResult', value: {tool: 'Bash', callId, content, isError: false}},
  });
}

function legacyToolChunkEvent(sequence: number, content: string): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    executionId: 'legacy-turn',
    data: {case: 'agentOutputChunk', value: {channel: AgentOutputChannel.TOOL, content}},
  });
}

function runStartedEvent(sequence: number): RunEvent {
  return makeEvent(EventType.RUN_STARTED, {
    sequence,
    timestamp: timestamp(sequence),
    status: EventStatus.ACTIVE,
    data: {case: 'runStarted', value: {outerLoop: 'plain', input: '/target', maxRounds: 3}},
  });
}

function roundFinishedEvent(sequence: number, extra: {profileSkipped?: boolean} = {}): RunEvent {
  return makeEvent(EventType.ROUND_FINISHED, {
    sequence,
    timestamp: timestamp(sequence),
    status: EventStatus.COMPLETED,
    roundLabel: 'round-1',
    data: {
      case: 'roundFinished',
      value: {attempts: 1, judgeVerdict: RoundJudgeVerdict.PASS, ...extra},
    },
  });
}

function executionStartedEvent(sequence: number, executionId: string): RunEvent {
  return baseEvent(sequence, EventType.AGENT_EXECUTION_STARTED, {
    roundLabel: 'round-1',
    executionId,
    status: EventStatus.ACTIVE,
    data: {
      case: 'agentExecutionStarted',
      value: {
        stage: 'implementation',
        systemPrompt: '',
        userPrompt: 'Implement the queue',
        activity: {mode: ExecutionActivityMode.THINKING, summary: 'Starting'},
      },
    },
  });
}

function executionFinishedEvent(sequence: number, executionId: string): RunEvent {
  return baseEvent(sequence, EventType.AGENT_EXECUTION_FINISHED, {
    roundLabel: 'round-1',
    executionId,
    status: EventStatus.COMPLETED,
    data: {case: 'agentExecutionFinished', value: {}},
  });
}

function phaseFinishedEvent(sequence: number, executionId: string): RunEvent {
  return baseEvent(sequence, EventType.PHASE_FINISHED, {
    roundLabel: 'round-1',
    executionId,
    status: EventStatus.COMPLETED,
    data: {case: 'phase', value: {phase: 'implementer'}},
  });
}

function legacyExecutionEvent(
  sequence: number,
  type: 'agent_execution_started' | 'agent_execution_finished',
): RunEvent {
  const event =
    type === 'agent_execution_started'
      ? executionStartedEvent(sequence, 'discarded')
      : executionFinishedEvent(sequence, 'discarded');
  return {...event, executionId: undefined};
}

function threadCreatedEvent(sequence: number, threadId: string): RunEvent {
  return baseEvent(sequence, EventType.CHAT_THREAD_CREATED, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    data: {
      case: 'chatThreadCreated',
      value: {
        threadId,
        title: '',
        driver: 'agentshim',
        provider: 'anthropic',
        model: 'opus',
        createdAt: timestamp(sequence),
      },
    },
  });
}

function chatEvent(
  sequence: number,
  threadId: string,
  answer: string,
  title?: string,
  invocationId?: string,
): RunEvent {
  return baseEvent(sequence, EventType.CHAT, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    status: EventStatus.ANSWERED,
    data: {case: 'chat', value: {answer, threadTitle: title, invocationId}},
  });
}

function chatChunkEvent(
  sequence: number,
  threadId: string,
  content: string,
  invocationId?: string,
): RunEvent {
  return baseEvent(sequence, EventType.AGENT_OUTPUT_CHUNK, {
    agentKind: 'chat',
    roundLabel: 'experiment-chat',
    chatThreadId: threadId,
    executionId: invocationId ?? `${threadId}-turn`,
    data: {case: 'agentOutputChunk', value: {channel: AgentOutputChannel.ASSISTANT, content}},
  });
}

function todoEvent(sequence: number, executionId: string, content: string): RunEvent {
  return baseEvent(sequence, EventType.TODO_UPDATE, {
    executionId,
    data: {case: 'todoUpdate', value: {todos: [{content, status: 'in_progress'}]}},
  });
}

function diagnosticEvent(
  sequence: number,
  id: string,
  severity: DiagnosticSeverity,
  summary: string,
  detail: string | undefined,
): RunEvent {
  return baseEvent(sequence, EventType.INVOCATION_FINISHED, {
    executionId: 'turn',
    diagnostic: {
      id,
      code: 'agent_failed',
      summary,
      detail,
      scope: DiagnosticScope.INVOCATION,
      severity,
      retryability: DiagnosticRetryability.MANUAL,
    },
  });
}

const TERMINAL_TYPES = {
  run_failed: EventType.RUN_FAILED,
  run_interrupted: EventType.RUN_INTERRUPTED,
  run_finished: EventType.RUN_FINISHED,
} as const;
