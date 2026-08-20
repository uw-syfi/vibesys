import {describe, expect, it} from 'bun:test';
import type {EventSubscription} from './client.js';
import type {ProtocolResponse, RequestInput, RunEvent, ServerMessage} from './protocol.js';
import {SocketSessionController, type SupervisionTransport} from './session-controller.js';
import {chatPaneVisible, experimentLogVisible} from './session-model.js';

describe('session controller', () => {
  it('shows local help without sending a backend command', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('/help');

    expect(controller.state.overlay?.kind).toBe('help');
    expect(controller.state.overlay?.content).toContain('/open-round');
    expect(controller.state.overlay?.content).toContain('Planned');
    expect(transport.requests).toEqual([]);
  });

  it('sends ordinary text to the chat docked beside the log', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('what is happening?');

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

    await controller.submit('/chat');

    // Already on screen: /chat puts the pane keys on it instead of opening a
    // modal over the log.
    expect(controller.state.chatOpen).toBe(false);
    expect(controller.state.layout.focus).toBe('chat');
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

  it('opens the chat as a modal where it cannot dock', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    // A terminal too narrow for two columns, reported by the renderer.
    controller.setChatDockFits(false);

    await controller.submit('what is happening?');

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

    await controller.submit('/help');
    expect(controller.state.overlay?.content).not.toContain('/chat');

    // Inside a hypothesis the chat is a dialog again, so the command returns.
    controller.enterExperimentDrilldown();
    await controller.submit('/help');
    expect(controller.state.overlay?.content).toContain('/chat');
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
    await controller.submit('/chat why did r1 fail?');
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

    await controller.submit('/chat why?');

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

  it('starts on the requested theme and defaults to dark', () => {
    expect(new SocketSessionController(new FakeTransport()).state.themeName).toBe('dark');
    expect(
      new SocketSessionController(new FakeTransport(), 'catppuccin-latte').state.themeName,
    ).toBe('catppuccin-latte');
  });

  it('opens the theme list as a selection starting on the active theme', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport, 'solarized-dark');

    await controller.submit('/theme');

    expect(controller.state.themePicker?.selected).toBe('solarized-dark');
    // The list is a selection, not a text overlay.
    expect(controller.state.overlay).toBeNull();
    expect(controller.state.themeName).toBe('solarized-dark');
    expect(transport.requests).toEqual([]);
  });

  it('applies the selected theme and closes the picker', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('/theme');
    controller.moveThemeSelection(2);
    controller.applySelectedTheme();

    expect(controller.state.themeName).toBe('solarized-dark');
    expect(controller.state.themePicker).toBeNull();
    expect(transport.requests).toEqual([]);
  });

  it('closes the picker without switching when it is dismissed', async () => {
    const controller = new SocketSessionController(new FakeTransport(), 'light');

    await controller.submit('/theme');
    controller.moveThemeSelection(1);
    controller.closeThemePicker();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
  });

  it('closes the picker when the selection is the theme already in use', async () => {
    const controller = new SocketSessionController(new FakeTransport(), 'light');

    await controller.submit('/theme');
    controller.applySelectedTheme();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
  });

  it('switches theme locally and closes the picker', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('/theme');
    await controller.submit('/theme high-contrast-dark');

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

    await controller.submit('/history');
    expect(controller.state.overlay?.kind).toBe('error');
    expect(controller.state.overlay?.content).toContain('Unknown command: /history');

    await controller.submit('/history rounds');
    expect(controller.state.overlay?.content).toContain('Unknown command: /history rounds');

    await controller.submit('/experiments');
    expect(controller.state.overlay?.content).toContain('Unknown command: /experiments');

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
    expect(controller.state.hypothesisScope).not.toBeNull();

    // What Ctrl+L and Escape are bound to.
    controller.live();

    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.entries).toHaveLength(1);
  });

  it('refetches the log when a round finishes and keeps the selected row', async () => {
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
    transport.emit({type: 'event', event: event(9, 'round_finished')});
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.length - before).toBe(1);
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
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.hypothesisScope?.label).toBe('H-02 · r2-3');

    controller.leaveExperimentDrilldown();
    expect(controller.state.hypothesisScope).toBeNull();
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
  });

  it('loads the log before the first frame so it can be the landing view', async () => {
    const transport = new FakeTransport();
    transport.experiments = [entry('H-01', 1, 1, {resolved_outcome: 'proven'})];
    const controller = new SocketSessionController(transport);

    expect(controller.state.experimentLog?.pending).toBe(true);
    await controller.start();

    expect(transport.requests).toEqual([{type: 'query.snapshot'}, {type: 'query.experiments'}]);
    expect(controller.state.experimentLog?.pending).toBe(false);
    expect(controller.state.experimentLog?.selectedId).toBe('H-01');
  });

  it('coalesces refetches when a burst of phase events lands', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);
    await controller.start();
    const before = transport.requests.length;

    transport.emit({
      type: 'event_batch',
      events: [event(1, 'phase_finished'), event(2, 'phase_finished'), event(3, 'round_finished')],
    });
    transport.emit({type: 'event', event: event(4, 'phase_finished')});
    await Promise.resolve();
    await Promise.resolve();

    expect(transport.requests.length - before).toBe(1);
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

    await controller.submit('/open-round');

    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.selectedRound).toBeNull();
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

    await controller.submit('/open-round --3');

    // Lands on the requested round, inside the hypothesis that owns it, and
    // moves the table selection to match.
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-02', rounds: [2, 3]});
    expect(controller.state.selectedRound).toBe(3);
    expect(controller.state.experimentLog?.selectedId).toBe('H-02');
  });

  it('reports a round that belongs to no hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();

    await controller.submit('/open-round --9');

    expect(controller.state.overlay?.content).toContain(
      'Round 9 is not in any recorded hypothesis.',
    );
    expect(controller.state.hypothesisScope).toBeNull();
  });

  it('says where it already is when /open-round runs inside a hypothesis', async () => {
    const transport = new FakeTransport();
    transport.experiments = [
      entry('H-01', 1, 1, {rounds: [{round: 1, passed: true, reviewed: true}]}),
    ];
    const controller = new SocketSessionController(transport);
    await controller.start();
    await controller.submit('/open-round');

    await controller.submit('/open-round');

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
    await controller.submit('/perf');
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
    await controller.submit('/perf');
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
    await controller.submit('/perf');

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

    await controller.submit('/perf');
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
    await controller.submit('/perf');
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
    await controller.submit('/chat');
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

  it('reports an unknown slash command in the chat instead of asking the agent', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submitChat('/nope');

    expect(controller.state.overlay?.kind).toBe('error');
    expect(controller.state.overlay?.content).toContain('Unknown command');
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

  it('reports an unknown theme as an error without switching', async () => {
    const transport = new FakeTransport();
    const controller = new SocketSessionController(transport);

    await controller.submit('/theme monokai');

    expect(controller.state.overlay?.kind).toBe('error');
    expect(controller.state.overlay?.content).toContain('Unknown theme: monokai');
    expect(controller.state.themeName).toBe('dark');
    expect(transport.requests).toEqual([]);
  });
});

class FakeTransport implements SupervisionTransport {
  closed = false;
  /** Mutable so a test can change what a refetch returns mid-run. */
  experiments: NonNullable<ProtocolResponse['experiments']> = [];
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
