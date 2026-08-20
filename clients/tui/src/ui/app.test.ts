import {afterEach, describe, expect, it} from 'bun:test';
import {InputRenderable, rgbToHex, ScrollBoxRenderable} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {HypothesisEntry} from '../protocol.js';
import type {SessionController} from '../session-controller.js';
import {
  closePane,
  closeThemePicker,
  cyclePaneFocus,
  enterExperimentDrilldown,
  enterExperimentRound,
  focusPane,
  initialSessionState,
  leaveExperimentDrilldown,
  moveExperimentSelection,
  moveThemeSelection,
  openExperimentLog,
  openPane,
  type PaneFocus,
  type PaneView,
  type SessionState,
  setChatDockFits,
  setExperiments,
  setPaneContent,
  setTheme,
} from '../session-model.js';
import {createOpenTuiApp, type OpenTuiApp} from './app.js';
import {resolveTheme, type ThemeName} from './theme.js';

const cleanup: Array<() => void> = [];

afterEach(() => {
  for (const destroy of cleanup.splice(0).reverse()) destroy();
});

describe('OpenTUI presentation', () => {
  it('renders model state with a persistent input panel', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      status: 'running',
      agentKind: 'optimizer',
      roundLabel: 'round 2',
      phases: [
        {
          kind: 'optimizer',
          status: 'active',
          roundNumber: null,
          roundLabel: 'round 2',
        },
      ],
      conversation: [
        {
          id: '1',
          kind: 'assistant',
          label: 'optimizer · round 2',
          agentKind: 'optimizer',
          content: '## Result\n\nUse `fast_path()`.',
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('fast_path()'));
    expect(frame).toContain('running · optimizer · round 2');
    // No round is selected, so the agent strip is headed by the run.
    expect(frame).toContain('Run flow');
    expect(frame).toContain('Rounds');
    expect(frame).toContain('● optimizer');
    expect(frame).toContain('Result');
    expect(frame).toContain('Ask or command');
    expect(frame).toContain('Type a question or /help');
  });

  it('renders quiet round labels without status text or symbols', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const activeStartedAt = new Date(Date.now() - 65_000).toISOString();
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [
        {number: 1, status: 'completed'},
        {
          number: 2,
          status: 'active',
          startedAt: activeStartedAt,
          activeAgentStarts: {'judge:judge-1': activeStartedAt},
        },
        {number: 3, status: 'failed'},
      ],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('r2'));

    expect(frame).toContain('r1');
    expect(frame).toContain('r2');
    expect(frame).toMatch(/r2\s+1m\s+\d+s/);
    expect(frame).toContain('r3');
    expect(frame).not.toMatch(/[◐◓◑◒]/);
    expect(frame).not.toContain('done');
    expect(frame).not.toContain(':run');
    expect(frame).not.toContain('fail');
  });

  it('heads the agent strip with the elapsed time of the running round', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const activeStartedAt = new Date(Date.now() - 65_000).toISOString();
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [
        {
          number: 2,
          status: 'active',
          startedAt: activeStartedAt,
          activeAgentStarts: {'judge:judge-1': activeStartedAt},
        },
      ],
      phases: [{kind: 'judge', status: 'active', roundNumber: 2, roundLabel: 'round-2-judge'}],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('Round 2 flow'));

    expect(frame).toMatch(/Round 2 flow · 1m \d+s/);
  });

  it('holds the agent-active elapsed time of a finished round', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [
        {
          number: 1,
          status: 'completed',
          // 60s of wall clock with a 15s gap where no agent was running.
          agentIntervals: [
            {startedAt: '2026-01-01T00:00:00Z', finishedAt: '2026-01-01T00:00:30Z'},
            {startedAt: '2026-01-01T00:00:45Z', finishedAt: '2026-01-01T00:01:00Z'},
          ],
        },
      ],
      phases: [{kind: 'judge', status: 'completed', roundNumber: 1, roundLabel: 'round-1-judge'}],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('Round 1 flow'));

    expect(frame).toContain('Round 1 flow · 45s');
  });

  it('omits the elapsed time for a round with no recorded agent time', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 18});
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [{number: 1, status: 'completed'}],
      phases: [{kind: 'judge', status: 'completed', roundNumber: 1, roundLabel: 'round-1-judge'}],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('Round 1 flow'));

    expect(frame).not.toContain('Round 1 flow ·');
  });

  it('submits typed commands when Enter is pressed', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/help');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/help']);
  });

  it('submits ordinary text as an operator question', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('what is running?');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['what is running?']);
  });

  it('suggests and completes slash commands with Tab', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/pa');
    const suggestions = await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    expect(suggestions).toContain('/pause');
    expect(suggestions).not.toContain('/help  ');
    expect(suggestions).not.toContain('/perf');
    expect(suggestions.indexOf('/pause')).toBeLessThan(suggestions.indexOf('Ask or command'));
    expect(testRenderer.renderer.root.findDescendantById('input-box')?.height).toBe(3);

    testRenderer.mockInput.pressKey('TAB');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/pause']);
  });

  it('highlights a leading slash-command token', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/steer inspect the cache');
    const input = testRenderer.renderer.root.findDescendantById('input');
    expect(input).toBeInstanceOf(InputRenderable);
    if (!(input instanceof InputRenderable)) throw new Error('input was not rendered');
    expect(input.getLineHighlights(0)).toMatchObject([{start: 0, end: 6}]);
  });

  it('exits on the first Ctrl-C even while the input is focused', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    cleanup.push(() => app.destroy());
    const destroyed = new Promise<void>(resolve => testRenderer.renderer.once('destroy', resolve));

    testRenderer.mockInput.pressKey('c', {ctrl: true});

    await destroyed;
  });

  it('advertises Escape and returns a non-live view to live output', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      overlay: {kind: 'help', content: 'Available commands'},
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const overlay = await testRenderer.waitForFrame(value => value.includes('Esc: close dialog'));
    expect(overlay).toContain('Available commands');
    expect(overlay).toContain('Rounds');
    expect(overlay).toContain('Agents');
    testRenderer.mockInput.pressKey('ESCAPE');
    await testRenderer.waitForFrame(value => !value.includes('Esc: close dialog'));
    expect(controller.liveCalls).toBe(1);
  });

  it('uses the native scrollbox for long output', async () => {
    const lines = Array.from({length: 50}, (_, index) => `tool output line ${index + 1}`).join(
      '\n',
    );
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      conversation: [{id: 'assistant', kind: 'assistant', label: 'Agent', content: lines}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('tool output line 50'));
    testRenderer.mockInput.pressKey('HOME');
    const frame = await testRenderer.waitForFrame(value => value.includes('tool output line 1'));
    expect(frame).not.toContain('tool output line 50');
  });

  it('renders a tool call and response as two regions in one card', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      conversation: [
        {
          id: 'tool',
          kind: 'tool',
          label: 'implementer · round 1',
          content: '→ Bash(command="pytest")\n2 passed',
          toolCall: '→ Bash(command="pytest")\n',
          toolResponse: '2 passed',
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('2 passed'));
    expect(frame).toContain('→ Bash(command="pytest")');
    expect(frame.match(/╭/g)).toHaveLength(5);
  });

  it('collapses prompts and expands the latest prompt with Ctrl+P', async () => {
    const content = Array.from({length: 20}, (_, index) => `prompt line ${index + 1}`).join('\n');
    const testRenderer = await createTestRenderer({width: 80, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      conversation: [{id: 'prompt', kind: 'prompt', label: 'Prompt', content}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const collapsed = await testRenderer.waitForFrame(value => value.includes('8 more lines'));
    expect(collapsed).not.toContain('prompt line 20');
    testRenderer.mockInput.pressKey('p', {ctrl: true});
    const expanded = await testRenderer.waitForFrame(value => value.includes('prompt line 20'));
    expect(expanded).toContain('collapse');
  });

  it('expands the latest visible prompt without dropping agent filters', async () => {
    const visiblePrompt = Array.from(
      {length: 20},
      (_, index) => `implementer prompt ${index + 1}`,
    ).join('\n');
    const hiddenPrompt = Array.from({length: 20}, (_, index) => `judge prompt ${index + 1}`).join(
      '\n',
    );
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      selectedRound: 1,
      selectedAgentKind: 'implementer',
      rounds: [{number: 1, status: 'active'}],
      conversation: [
        {
          id: 'implementer-prompt',
          kind: 'prompt',
          agentKind: 'implementer',
          roundNumber: 1,
          content: visiblePrompt,
        },
        {
          id: 'judge-prompt',
          kind: 'prompt',
          agentKind: 'judge',
          roundNumber: 1,
          content: hiddenPrompt,
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('implementer prompt 1'));
    testRenderer.mockInput.pressKey('p', {ctrl: true});
    const expanded = await testRenderer.waitForFrame(value =>
      value.includes('implementer prompt 20'),
    );
    expect(expanded).not.toContain('judge prompt');
  });

  it('preserves existing cards when a large transcript receives state-only and tail updates', async () => {
    const conversation = Array.from({length: 1_000}, (_, index) => ({
      id: `entry-${index}`,
      kind: 'status' as const,
      content: `event ${index}`,
    }));
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({...initialSessionState(), conversation});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('event 999'));
    const firstCard = testRenderer.renderer.root.findDescendantById('event-entry-0');
    const lastCard = testRenderer.renderer.root.findDescendantById('event-entry-999');

    controller.publish({...controller.state, status: 'paused'});
    expect(testRenderer.renderer.root.findDescendantById('event-entry-0')).toBe(firstCard);
    expect(testRenderer.renderer.root.findDescendantById('event-entry-999')).toBe(lastCard);

    const previousLast = conversation.at(-1);
    if (previousLast === undefined) throw new Error('large transcript is unexpectedly empty');
    const updatedLast = {...previousLast, content: 'updated tail'};
    controller.publish({
      ...controller.state,
      conversation: [...conversation.slice(0, -1), updatedLast],
    });
    await testRenderer.waitForFrame(value => value.includes('updated tail'));
    expect(testRenderer.renderer.root.findDescendantById('event-entry-0')).toBe(firstCard);
    expect(testRenderer.renderer.root.findDescendantById('event-entry-999')).not.toBe(lastCard);
  });

  it('selects an agent with Tab and filters the transcript', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      phases: [
        {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1'},
        {kind: 'judge', status: 'active', roundNumber: 1, roundLabel: 'round-1'},
      ],
      rounds: [{number: 1, status: 'active'}],
      conversation: [
        {
          id: 'implementer',
          kind: 'assistant',
          label: 'implementer · round 1',
          agentKind: 'implementer',
          roundNumber: 1,
          content: 'edited files',
        },
        {
          id: 'judge',
          kind: 'assistant',
          label: 'judge · round 1',
          agentKind: 'judge',
          roundNumber: 1,
          content: 'checking behavior',
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('checking behavior'));
    testRenderer.mockInput.pressKey('TAB');
    const filtered = await testRenderer.waitForFrame(value =>
      value.includes('selected implementer'),
    );
    expect(filtered).toContain('edited files');
    expect(filtered).not.toContain('checking behavior');
  });

  it('summarizes the active agent’s todos and expands them with Ctrl+T', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      agentKind: 'implementer',
      todoPhases: [
        {
          agentKind: 'implementer',
          roundNumber: null,
          items: [
            {content: 'Profile the hot loop', status: 'completed'},
            {content: 'Vectorize the kernel', status: 'in_progress'},
            {content: 'Re-run the benchmark', status: 'pending'},
          ],
        },
      ],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const collapsed = await testRenderer.waitForFrame(value => value.includes('Todo 1/3'));
    expect(collapsed).toContain('▶ Vectorize the kernel');
    expect(collapsed).not.toContain('Re-run the benchmark');

    testRenderer.mockInput.pressKey('t', {ctrl: true});
    const expanded = await testRenderer.waitForFrame(value =>
      value.includes('Re-run the benchmark'),
    );
    expect(expanded).toContain('✓ Profile the hot loop');
    expect(expanded).toContain('○ Re-run the benchmark');
  });

  it('hides the todo strip when the visible agent has no todos', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      agentKind: 'judge',
      todoPhases: [
        {
          agentKind: 'implementer',
          roundNumber: null,
          items: [{content: 'Edit files', status: 'completed'}],
        },
      ],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('live output'));
    expect(frame).not.toContain('Todo');
    expect(frame).not.toContain('Edit files');
  });

  it('keeps terminal results visible until the operator exits', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      status: 'failed',
      terminal: true,
      conversation: [
        {
          id: 'configuration-error',
          kind: 'result',
          label: 'Configuration failed',
          content: 'Invalid --max-rounds value',
          tone: 'failure',
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    let destroyed = false;
    testRenderer.renderer.once('destroy', () => {
      destroyed = true;
    });

    const frame = await testRenderer.waitForFrame(value =>
      value.includes('Invalid --max-rounds value'),
    );
    expect(frame).toContain('Configuration failed');
    await new Promise(resolve => setTimeout(resolve, 150));
    expect(destroyed).toBe(false);
  });

  it('opens a focused chat popup after a configuration failure', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      status: 'failed',
      terminal: true,
      conversation: [
        {
          id: 'configuration-error',
          kind: 'result',
          label: 'Configuration failed',
          content: 'agent.toml was not found',
          tone: 'failure',
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/chat');
    testRenderer.mockInput.pressEnter();
    const popup = await testRenderer.waitForFrame(value => value.includes('Experiment chat'));
    expect(popup).toContain('Ask about this experiment');
    expect(popup).toContain('Message');

    await testRenderer.mockInput.typeText('why did startup fail?');
    testRenderer.mockInput.pressEnter();
    const answer = await testRenderer.waitForFrame(value => value.includes('Recorded diagnostic'));
    expect(controller.chatSubmissions).toEqual(['why did startup fail?']);
    expect(answer).toContain('Inspecting configuration events');
    expect(answer).toContain('→ Read(run-events.jsonl)');

    testRenderer.mockInput.pressKey('ESCAPE');
    await testRenderer.waitForFrame(value => !value.includes('Experiment chat'));
    expect(controller.state.chatOpen).toBe(false);
  });

  it('accepts another chat message while an agent turn is pending', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      chatPending: true,
      chatConversation: [
        {id: 'active-question', kind: 'user', label: 'You', content: 'first question'},
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/chat');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Experiment chat'));
    await testRenderer.mockInput.typeText('queued follow-up');
    testRenderer.mockInput.pressEnter();

    await testRenderer.waitForFrame(value => value.includes('Recorded diagnostic'));
    expect(controller.chatSubmissions).toEqual(['queued follow-up']);
  });
});

describe('theming', () => {
  const assistantEntry = {
    id: 'themed',
    kind: 'assistant' as const,
    label: 'implementer',
    agentKind: 'implementer',
    content: 'themed body text',
  };

  it('paints the whole surface from the selected theme', async () => {
    const light = resolveTheme('light');
    const testRenderer = await createTestRenderer({width: 90, height: 20});
    const controller = new FakeController({
      ...initialSessionState('light'),
      status: 'running',
      conversation: [assistantEntry],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('themed body text'));

    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(light.accent);
    const body = spanColors(testRenderer, 'themed body text');
    expect(body?.fg).toBe(light.conversation.assistant.content);
    expect(body?.bg).toBe(light.conversation.assistant.background);
    expect(spanColors(testRenderer, 'implementer')?.fg).toBe(light.conversation.assistant.label);
  });

  it('keeps the dark baseline identical to the pre-theme palette', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      status: 'running',
      conversation: [assistantEntry],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('themed body text'));

    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe('#22d3ee');
    const body = spanColors(testRenderer, 'themed body text');
    expect(body?.fg).toBe('#e2e8f0');
    expect(body?.bg).toBe('#0f1b24');
    expect(spanColors(testRenderer, 'implementer')?.fg).toBe('#67e8f9');
  });

  it('repaints live when the selected theme changes', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 20});
    const controller = new FakeController({
      ...initialSessionState(),
      status: 'running',
      conversation: [assistantEntry],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('themed body text'));
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(resolveTheme('dark').accent);

    controller.setTheme('solarized-light');
    await testRenderer.waitForVisualIdle();

    const solarized = resolveTheme('solarized-light');
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(solarized.accent);
    const body = spanColors(testRenderer, 'themed body text');
    expect(body?.fg).toBe(solarized.conversation.assistant.content);
    expect(body?.bg).toBe(solarized.conversation.assistant.background);
  });

  it('navigates the theme list with the keyboard and applies on Enter', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [
        {number: 1, status: 'completed'},
        {number: 2, status: 'active'},
      ],
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/theme');
    testRenderer.mockInput.pressEnter();
    const opened = await testRenderer.waitForFrame(value => value.includes('Themes'));
    // The view opens on the theme in use.
    expect(opened).toContain('\u203a dark');
    expect(opened).toContain('active');

    testRenderer.mockInput.pressKey('ARROW_DOWN');
    const moved = await testRenderer.waitForFrame(value => value.includes('\u203a light'));
    expect(moved).not.toContain('\u203a dark');
    // Navigating the list leaves the view behind it alone.
    expect(controller.state.selectedRound).toBeNull();
    expect(controller.state.selectedAgentKind).toBeNull();
    expect(controller.state.themeName).toBe('dark');

    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForVisualIdle();

    expect(controller.state.themeName).toBe('light');
    expect(controller.state.themePicker).toBeNull();
    expect(spanColors(testRenderer, 'VibeSys')?.fg).toBe(resolveTheme('light').accent);
  });

  it('closes the theme list on Escape without switching theme', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      conversation: [{id: 'live', kind: 'assistant', label: 'Agent', content: 'live output'}],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/theme');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Themes'));
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('\u203a light'));

    testRenderer.mockInput.pressKey('ESCAPE');
    // A bare ESC is held by the stdin parser until its escape-sequence
    // timeout expires, so the key lands a beat after it is pressed.
    await new Promise(resolve => setTimeout(resolve, 40));
    await testRenderer.flush();
    const closed = testRenderer.captureCharFrame();

    expect(closed).not.toContain('\u203a light');
    expect(closed).toContain('live output');
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.themeName).toBe('dark');
    // Escape closed the picker rather than resetting the view behind it.
    expect(controller.liveCalls).toBe(0);
  });

  it('leaves Enter to a typed command while the theme list is open', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 24});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.mockInput.typeText('/theme');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(value => value.includes('Themes'));
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('\u203a light'));

    await testRenderer.mockInput.typeText('/help');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 2);

    // The command ran; the highlighted theme was not applied by its Enter.
    expect(controller.submissions).toEqual(['/theme', '/help']);
    expect(controller.state.themeName).toBe('dark');
    expect(controller.state.themePicker?.selected).toBe('light');
  });

  it('owns its keys over the chat it was opened from', async () => {
    const testRenderer = await createTestRenderer({width: 90, height: 26});
    const controller = new FakeController({...initialSessionState(), chatOpen: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await testRenderer.waitForFrame(value => value.includes('Ask a question about'));

    await controller.submitChat('/theme');
    await testRenderer.waitForFrame(value => value.includes('Themes'));
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await testRenderer.waitForFrame(value => value.includes('\u203a light'));

    // The chat is still open behind the picker, and neither the arrows nor
    // Escape reached it.
    expect(controller.state.chatOpen).toBe(true);
    testRenderer.mockInput.pressKey('ESCAPE');
    // A bare ESC is held by the stdin parser until its escape-sequence
    // timeout expires, so the key lands a beat after it is pressed.
    await new Promise(resolve => setTimeout(resolve, 40));
    await testRenderer.flush();
    expect(testRenderer.captureCharFrame()).not.toContain('\u203a light');
    expect(controller.state.themePicker).toBeNull();
    expect(controller.state.chatOpen).toBe(true);
    expect(controller.chatSubmissions).toEqual([]);
  });

  it('themes the overlay and the chat panel from the same theme', async () => {
    const latte = resolveTheme('catppuccin-latte');
    const testRenderer = await createTestRenderer({width: 90, height: 30});
    const controller = new FakeController(initialSessionState('catppuccin-latte'));
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    controller.publish({
      ...controller.state,
      overlay: {kind: 'error', content: 'configuration failed'},
    });
    await testRenderer.waitForFrame(value => value.includes('configuration failed'));
    expect(spanColors(testRenderer, 'configuration failed')?.fg).toBe(
      latte.conversation.failure.content,
    );
    expect(spanColors(testRenderer, 'Esc to close')?.fg).toBe(latte.textSubtle);

    controller.publish({...controller.state, overlay: null, chatOpen: true});
    await testRenderer.waitForFrame(value => value.includes('Ask a question about'));
    expect(spanColors(testRenderer, 'Ask a question about')?.fg).toBe(latte.textSubtle);
  });

  it('conveys agent and todo status with glyphs and words, not color alone', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 36});
    const controller = new FakeController({
      ...initialSessionState('high-contrast-light'),
      rounds: [{number: 1, status: 'active'}],
      phases: [
        {kind: 'implementer', status: 'completed', roundNumber: 1, roundLabel: 'round-1'},
        {kind: 'judge', status: 'failed', roundNumber: 1, roundLabel: 'round-1'},
      ],
      todoPhases: [
        {
          agentKind: null,
          roundNumber: null,
          items: [
            {content: 'write the kernel', status: 'completed'},
            {content: 'benchmark it', status: 'in_progress'},
          ],
        },
      ],
      todosExpanded: true,
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    const frame = await testRenderer.waitForFrame(value => value.includes('benchmark it'));

    expect(frame).toContain('✓ implementer');
    expect(frame).toContain('× judge');
    expect(frame).toContain('completed');
    expect(frame).toContain('failed');
    expect(frame).toContain('✓ write the kernel');
    expect(frame).toContain('▶ benchmark it');
  });
  it('lands on the experiment log instead of the per-round transcript', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 22});
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [{number: 41, status: 'completed'}],
      conversation: [
        {
          id: 'a',
          kind: 'assistant',
          label: 'implementer',
          content: 'round 41 detail',
          roundNumber: 41,
        },
      ],
    });
    // The landing view is what a fresh client starts on.
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        judge_verdict: 'pass',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const landing = await frameAfter(testRenderer);
    expect(landing).toContain('Experiments');
    expect(landing).toContain('Implementation Details');
    expect(landing).toContain('H-07');
    // Per-round detail is what the operator opts into, not what greets them.
    expect(landing).not.toContain('round 41 detail');
    // The rounds strip and agent map are per-round chrome; neither is drawn.
    expect(landing).not.toContain('─ Rounds ─');
    expect(landing).not.toContain('Agents');
  });

  it('opens the round trajectory behind a hypothesis and steps back out', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController({
      ...initialSessionState(),
      rounds: [
        {number: 41, status: 'completed'},
        {number: 42, status: 'completed'},
        {number: 43, status: 'completed'},
      ],
      conversation: [
        {
          id: 'a',
          kind: 'assistant',
          label: 'implementer',
          content: 'unrelated round 41',
          roundNumber: 41,
        },
        {
          id: 'b',
          kind: 'assistant',
          label: 'implementer',
          content: 'grew the block',
          roundNumber: 42,
        },
        {id: 'c', kind: 'assistant', label: 'judge', content: 'regression found', roundNumber: 43},
      ],
    });
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
      logEntry('H-08', 42, 43, {
        claim: 'bigger KV cache block',
        resolved_outcome: 'rejected',
        rounds: [
          {round: 42, passed: false, reviewed: false},
          {round: 43, passed: false, reviewed: true},
        ],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    const table = await frameAfter(testRenderer);
    expect(table).toContain('42-43');
    expect(table).not.toContain('grew the block');

    testRenderer.mockInput.pressKey('ARROW_DOWN');
    testRenderer.mockInput.pressEnter();
    const trajectory = await frameAfter(testRenderer);

    // Every round of that hypothesis, and only those rounds.
    expect(trajectory).toContain('grew the block');
    expect(trajectory).toContain('regression found');
    expect(trajectory).not.toContain('unrelated round 41');
    expect(trajectory).toContain('H-08 · r42-43');
    expect(trajectory).toContain('r42');
    expect(trajectory).toContain('r43');
    expect(trajectory).not.toContain('r41');
    expect(controller.state.hypothesisScope).toMatchObject({id: 'H-08', rounds: [42, 43]});

    testRenderer.mockInput.pressKey('ESCAPE');
    const back = await frameAfterEscape(testRenderer);
    expect(back).toContain('Implementation Details');
    expect(back).not.toContain('grew the block');
    expect(controller.state.experimentLog?.selectedId).toBe('H-08');
  });

  it('runs a typed command from the log on the first Enter', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController(initialSessionState());
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/help');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);

    // One Enter runs the command; the table is still the view behind it.
    expect(controller.submissions).toEqual(['/help']);
    expect(controller.state.hypothesisScope).toBeNull();
    expect(await frameAfter(testRenderer)).toContain('Implementation Details');
  });

  it('opens a command overlay on the log and leaves the table behind it', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 24});
    const controller = new FakeController(initialSessionState());
    controller.experiments = [
      logEntry('H-07', 41, 41, {
        claim: 'batch the prefill step',
        resolved_outcome: 'proven',
        rounds: [{round: 41, passed: true, reviewed: true}],
      }),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      overlay: {kind: 'help', content: 'Available commands'},
    });

    const overlaid = await frameAfter(testRenderer);
    expect(overlaid).toContain('Available commands');
    expect(controller.state.experimentLog).not.toBeNull();

    // Enter behind an overlay must not move the operator somewhere unseen.
    testRenderer.mockInput.pressEnter();
    await frameAfter(testRenderer);
    expect(controller.state.hypothesisScope).toBeNull();

    testRenderer.mockInput.pressKey('ESCAPE');
    const back = await frameAfterEscape(testRenderer);
    expect(back).toContain('Implementation Details');
    expect(back).not.toContain('Available commands');
  });

  it('colors the outcome cell from the active theme in light and dark', async () => {
    for (const name of ['dark', 'light'] as const) {
      const theme = resolveTheme(name);
      const testRenderer = await createTestRenderer({width: 120, height: 18});
      const controller = new FakeController(initialSessionState(name));
      controller.experiments = [
        logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'proven'}),
        logEntry('H-08', 42, 43, {claim: 'bigger KV cache block', resolved_outcome: 'disproven'}),
        logEntry('H-09', 44, 44, {
          claim: 'retry with tuning',
          resolved_outcome: null,
          active: true,
        }),
      ];
      const app = createOpenTuiApp(testRenderer.renderer, controller);
      registerCleanup(testRenderer.renderer, app);
      await controller.openExperimentLog();
      await frameAfter(testRenderer);

      expect(spanColors(testRenderer, 'Proven')?.fg).toBe(theme.success);
      expect(spanColors(testRenderer, 'Disproven')?.fg).toBe(theme.error);
      expect(spanColors(testRenderer, 'Active')?.fg).toBe(theme.warning);
      // The claim keeps body text: only the resolution is colored.
      expect(spanColors(testRenderer, 'Batch the prefill step')?.fg).toBe(theme.textPrimary);
    }
  });

  it('scrolls a log taller than the panel and keeps ordering stable', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 20});
    const controller = new FakeController(initialSessionState());
    controller.experiments = Array.from({length: 120}, (_, index) =>
      logEntry(`H-${String(index + 1).padStart(3, '0')}`, index + 1, index + 1, {
        resolved_outcome: index % 2 === 0 ? 'proven' : 'rejected',
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const first = await frameAfter(testRenderer);
    expect(first).toContain('1/120');
    expect(first).not.toContain('H-120');

    // No named page key in the mock input; send the raw terminal sequence.
    for (let index = 0; index < 12; index += 1) testRenderer.mockInput.pressKey('\x1B[6~');
    const scrolled = await frameAfter(testRenderer);
    expect(scrolled).toContain('120/120');
    expect(scrolled).not.toContain('H-001');
  });

  it('scrolls the log with the wheel, independently of the selection', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 18});
    const controller = new FakeController(initialSessionState());
    controller.experiments = Array.from({length: 60}, (_, index) =>
      logEntry(`H-${String(index + 1).padStart(3, '0')}`, index + 1, index + 1, {
        resolved_outcome: 'proven',
      }),
    );
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    const top = await frameAfter(testRenderer);
    expect(top).toContain('H-001');

    const rows = testRenderer.renderer.root.findDescendantById('experiment-rows');
    if (!(rows instanceof ScrollBoxRenderable)) throw new Error('rows were not a scroll box');
    for (let index = 0; index < 20; index += 1) {
      await testRenderer.mockMouse.scroll(60, 8, 'down');
    }
    const scrolled = await frameAfter(testRenderer);

    expect(rows.scrollTop).toBeGreaterThan(0);
    expect(scrolled).not.toContain('H-001');
    // The wheel moves the viewport, not the cursor.
    expect(controller.state.experimentLog?.selectedId).toBe('H-001');
  });

  it('lands with the chat docked beside the hypothesis table', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const landing = await frameAfter(testRenderer);

    // Both columns at once, and the table keeps the claim it came to show.
    expect(landing).toContain('Experiment chat');
    expect(landing).toContain('Experiments');
    expect(landing).toContain('Implementation Details');
    expect(landing).toContain('Batch the prefill step');

    // Each column has its own input, under the surface it writes to, and the
    // command box starts where the chat column ends rather than running under
    // it.
    // The cursor starts in the command box, and the chat says how to reach it.
    expect(landing).toContain('Ctrl+W to type here');
    const lines = landing.split('\n');
    const paneTop = lines.find(line => line.includes('╭─ Experiment chat')) ?? '';
    const inputTop = lines.find(line => line.includes('╭─ Chat ')) ?? '';
    expect(inputTop).toContain('╭─ Ask or command');
    expect(inputTop.indexOf('╭─ Ask or command')).toBe(paneTop.indexOf('╭─ Experiments'));
  });

  it('routes typing to whichever input Ctrl+W points at', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('why is r41 slow?');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.chatSubmissions.length === 1);

    // The chat's own box took it, not the command input.
    expect(controller.chatSubmissions).toEqual(['why is r41 slow?']);
    expect(controller.submissions).toEqual([]);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    await testRenderer.mockInput.typeText('/perf');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);

    expect(controller.submissions).toEqual(['/perf']);
    expect(controller.chatSubmissions).toHaveLength(1);
  });

  it('raises the command list out of the command input, clear of the chat', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/pe');
    const frame = await testRenderer.waitForFrame(value => value.includes('[Tab]'));

    const lines = frame.split('\n');
    const suggestion = lines.find(line => line.includes('/perf')) ?? '';
    const commandInput = lines.find(line => line.includes('╭─ Ask or command')) ?? '';
    // The list belongs to the box it completes, so it starts where that box
    // starts rather than running back across the chat column.
    expect(suggestion.indexOf('│')).toBe(commandInput.indexOf('╭─ Ask or command'));
  });

  it('drops /chat from the command surface while the chat is already docked', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    await testRenderer.mockInput.typeText('/c');
    const frame = await frameAfter(testRenderer);

    // Nothing to open: the chat is the column beside the table.
    expect(frame).not.toContain('/chat');
  });

  it('says when the docked chat is waiting on the agent', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    controller.publish({...controller.state, chatPending: true});

    expect(await frameAfter(testRenderer)).toContain('Awaiting the agent');
  });

  it('answers in the docked chat without covering the table', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    await testRenderer.mockInput.typeText('why is r41 slow?');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    controller.publish({
      ...controller.state,
      chatConversation: [
        {id: 'q', kind: 'user', label: 'You', content: 'why is r41 slow?'},
        {id: 'a', kind: 'assistant', label: 'Answer', content: 'Prefill dominates.'},
      ],
    });

    const answered = await frameAfter(testRenderer);

    expect(answered).toContain('Prefill dominates.');
    expect(answered).toContain('H-07');
    expect(answered).toContain('Implementation Details');
  });

  it('keeps the chat, the table, and the visualization on screen together', async () => {
    const testRenderer = await createTestRenderer({width: 200, height: 20});
    const controller = logController();
    controller.paneContent = 'Performance · tok_s\n    1135 ┤   ●\nbest r7 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    controller.publish({
      ...controller.state,
      chatConversation: [{id: 'a', kind: 'assistant', label: 'Answer', content: 'Prefill.'}],
    });

    await controller.openPane('perf');
    const frame = await frameAfter(testRenderer);

    // Three columns: chat, table, visualization. None replaced another.
    expect(frame).toContain('Experiment chat');
    expect(frame).toContain('H-07');
    expect(frame).toContain('best r7 1135 tok_s');
    expect(frame).toContain('Ctrl+W: switch pane');
  });

  it('gives the row to the table alone when the chat cannot fit beside it', async () => {
    const testRenderer = await createTestRenderer({width: 84, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const landing = await frameAfter(testRenderer);

    // Two columns would both be unreadable here, so the table keeps the row.
    expect(landing).not.toContain('Experiment chat');
    expect(landing).toContain('H-07');
    expect(controller.state.chatDockFits).toBe(false);
  });

  it('keeps the table behind the chat modal instead of the round transcript', async () => {
    const testRenderer = await createTestRenderer({width: 84, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    // What a question does where the chat cannot dock.
    controller.publish({...controller.state, chatOpen: true});

    const frame = await frameAfter(testRenderer);

    expect(frame).toContain('Experiment chat');
    expect(frame).toContain('H-07');
    // The per-round chrome belongs to a hypothesis the operator never opened.
    expect(frame).not.toContain('─ Rounds ─');
    expect(frame).not.toContain('─ Agents ─');
  });

  it('moves the pane keys onto the docked chat and back with Ctrl+W', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    const focused = await frameAfter(testRenderer);
    expect(focused).toContain('▸ Experiment chat');
    expect(focused).toContain('Ask about this run');
    expect(controller.state.layout.focus).toBe('chat');

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    await frameAfter(testRenderer);
    expect(controller.state.layout.focus).toBe('left');
  });

  it('leaves the table its own keys while the chat is docked', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = logController();
    controller.experiments = [
      logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'proven'}),
      logEntry('H-08', 42, 42, {claim: 'bigger KV cache block', resolved_outcome: 'disproven'}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await frameAfter(testRenderer);

    // Arrows still belong to the table, docked chat or not.
    testRenderer.mockInput.pressKey('ARROW_DOWN');
    await frameAfter(testRenderer);
    expect(controller.state.experimentLog?.selectedId).toBe('H-08');
  });

  it('renders the transcript and the visualization side by side', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await controller.openPane('perf');
    const frame = await frameAfter(testRenderer);

    // Both live at once; neither obscures the other.
    expect(frame).toContain('batched the prefill step');
    expect(frame).toContain('best r7 1135 tok_s');
    expect(frame).toContain('Performance');
    expect(frame).toContain('Ctrl+W: switch pane');
  });

  it('moves focus between panes and shows which one has it', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');

    const onRight = await frameAfter(testRenderer);
    expect(onRight).toContain('· focused');
    const theme = resolveTheme('dark');
    expect(spanColors(testRenderer, 'Performance · focused')?.fg).toBe(theme.borderFocus);

    testRenderer.mockInput.pressKey('w', {ctrl: true});
    const onLeft = await frameAfter(testRenderer);

    expect(controller.state.layout.focus).toBe('left');
    expect(onLeft).not.toContain('· focused');
  });

  it('closes the pane with Escape and restores the full-width transcript', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    await frameAfter(testRenderer);

    testRenderer.mockInput.pressKey('ESCAPE');
    const frame = await frameAfterEscape(testRenderer);

    expect(controller.state.layout.right).toBeNull();
    expect(frame).not.toContain('best r7 1135 tok_s');
    expect(frame).toContain('batched the prefill step');
    expect(frame).toContain('Agents');
  });

  it('falls back to the modal below the split threshold and recovers on resize', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    const wide = await frameAfter(testRenderer);
    expect(wide).toContain('Ctrl+W: switch pane');

    testRenderer.renderer.resize(80, 20);
    const narrow = await frameAfter(testRenderer);

    // Single view, chart in the modal it used before the split existed.
    expect(narrow).toContain('best r7 1135 tok_s');
    expect(narrow).toContain('Command');
    expect(narrow).not.toContain('Ctrl+W: switch pane');

    testRenderer.renderer.resize(140, 20);
    const recovered = await frameAfter(testRenderer);

    expect(recovered).toContain('Ctrl+W: switch pane');
    expect(recovered).toContain('batched the prefill step');
  });

  it('puts a visualization beside the experiment log rather than replacing it', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = new FakeController(initialSessionState());
    controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
    controller.experiments = [
      logEntry('H-07', 41, 41, {claim: 'batch the prefill step', resolved_outcome: 'proven'}),
    ];
    controller.paneContent = 'Performance · tok_s\nbest r41 1135 tok_s';
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();
    await controller.openPane('perf');

    const frame = await frameAfter(testRenderer);

    expect(frame).toContain('H-07');
    expect(frame).toContain('best r41 1135 tok_s');
    // The table gives up its widest column to make room, rather than vanishing.
    expect(frame).toContain('Hypothesis');
  });

  it('styles both panes and the focus indicator from the selected theme', async () => {
    const theme = resolveTheme('solarized-light');
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    controller.publish({...controller.state, themeName: 'solarized-light'});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    await frameAfter(testRenderer);

    expect(spanColors(testRenderer, 'Performance · focused')?.fg).toBe(theme.borderFocus);
    expect(spanColors(testRenderer, 'best r7 1135 tok_s')?.fg).toBe(theme.textPrimary);

    controller.cyclePaneFocus();
    await frameAfter(testRenderer);

    // Focus moved to the transcript, so the pane border drops back to the
    // ordinary border colour and the transcript takes the focus colour.
    expect(spanColors(testRenderer, 'Performance')?.fg).toBe(theme.border);
  });

  it('keeps the pane current while the run advances', async () => {
    const testRenderer = await createTestRenderer({width: 140, height: 20});
    const controller = splitController();
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openPane('perf');
    expect(await frameAfter(testRenderer)).toContain('best r7 1135 tok_s');

    // A later round lands and the controller refetches; the pane redraws in
    // place rather than needing to be reopened.
    controller.paneContent = 'Performance · tok_s\n    1180 ┤    ●\nbest r8 1180 tok_s';
    await controller.openPane('perf');
    const updated = await frameAfter(testRenderer);

    expect(updated).toContain('best r8 1180 tok_s');
    expect(updated).not.toContain('best r7 1135 tok_s');
  });

  it('shows an explicit placeholder for records with no hypothesis id', async () => {
    const testRenderer = await createTestRenderer({width: 120, height: 16});
    const controller = new FakeController(initialSessionState());
    controller.experiments = [
      logEntry('(unidentified)', 1, 1, {identified: false, claim: null, resolved_outcome: null}),
    ];
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('—');
  });

  it('says so plainly when a run has no hypotheses yet', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController(initialSessionState());
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    await controller.openExperimentLog();

    const frame = await frameAfter(testRenderer);
    expect(frame).toContain('No hypotheses have been recorded yet.');
    expect(frame).toContain('once the orchestrator has planned a round');
    // The log is the root view: nothing offers a way out of it.
    expect(frame).not.toContain('Esc');
  });
});

/**
 * The experiment log settles synchronously, so there is no later frame to wait
 * for. Flush pending work, then read the single settled frame.
 */
async function frameAfter(testRenderer: TestRendererSetup): Promise<string> {
  await testRenderer.flush();
  return testRenderer.captureCharFrame();
}

/**
 * A bare ESC is held by the stdin parser until its escape-sequence timeout
 * expires, so the key lands a beat after it is pressed.
 */
async function frameAfterEscape(testRenderer: TestRendererSetup): Promise<string> {
  await new Promise(resolve => setTimeout(resolve, 40));
  return frameAfter(testRenderer);
}

/** A client on the landing view, with one hypothesis to show. */
function logController(): FakeController {
  const controller = new FakeController({
    ...initialSessionState(),
    status: 'running',
    rounds: [{number: 41, status: 'completed'}],
  });
  controller.publish({...controller.state, experimentLog: initialSessionState().experimentLog});
  controller.experiments = [
    logEntry('H-07', 41, 41, {
      claim: 'batch the prefill step',
      resolved_outcome: 'proven',
      judge_verdict: 'pass',
      rounds: [{round: 41, passed: true, reviewed: true}],
    }),
  ];
  return controller;
}

function splitController(): FakeController {
  const controller = new FakeController({
    ...initialSessionState(),
    status: 'running',
    rounds: [{number: 7, status: 'active'}],
    conversation: [
      {
        id: 'a',
        kind: 'assistant',
        label: 'implementer · round 7',
        content: 'batched the prefill step',
        roundNumber: 7,
      },
    ],
  });
  controller.paneContent = 'Performance · tok_s\n    1135 ┤   ●\nbest r7 1135 tok_s';
  return controller;
}

function logEntry(
  id: string,
  firstRound: number,
  lastRound: number,
  overrides: Partial<HypothesisEntry> = {},
): HypothesisEntry {
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

function spanColors(
  testRenderer: TestRendererSetup,
  needle: string,
): {fg: string; bg: string} | undefined {
  for (const line of testRenderer.captureSpans().lines) {
    for (const span of line.spans) {
      if (span.text.includes(needle)) {
        return {fg: rgbToHex(span.fg).toLowerCase(), bg: rgbToHex(span.bg).toLowerCase()};
      }
    }
  }
  return undefined;
}

function registerCleanup(
  renderer: Awaited<ReturnType<typeof createTestRenderer>>['renderer'],
  app: OpenTuiApp,
): void {
  cleanup.push(() => {
    app.destroy();
    renderer.destroy();
  });
}

class FakeController implements SessionController {
  readonly #listeners = new Set<(state: SessionState) => void>();
  readonly submissions: string[] = [];
  readonly chatSubmissions: string[] = [];
  liveCalls = 0;

  /**
   * Tests that exercise the transcript start past the landing view. The
   * experiment log has its own tests that open it explicitly.
   */
  constructor(state: SessionState) {
    this.state = {...state, experimentLog: null};
  }

  state: SessionState;

  publish(state: SessionState): void {
    this.state = state;
    for (const listener of this.#listeners) listener(this.state);
  }

  start(): Promise<void> {
    return Promise.resolve();
  }
  stop(): Promise<void> {
    return Promise.resolve();
  }
  submit(value: string): Promise<void> {
    this.submissions.push(value);
    if (value.trim() === '/chat') {
      this.state = {...this.state, chatOpen: true, overlay: null};
      this.#notify();
    }
    if (value.trim() === '/theme') this.openThemePicker();
    return Promise.resolve();
  }
  closeChat(): void {
    this.state = {...this.state, chatOpen: false};
    this.#notify();
  }
  setTheme(themeName: ThemeName): void {
    this.state = {...this.state, themeName};
    this.#notify();
  }
  openThemePicker(): void {
    this.publish({...this.state, overlay: null, themePicker: {selected: this.state.themeName}});
  }
  moveThemeSelection(delta: number): void {
    this.publish(moveThemeSelection(this.state, delta));
  }
  applySelectedTheme(): void {
    const picker = this.state.themePicker;
    if (picker !== null) this.publish(setTheme(this.state, picker.selected));
  }
  closeThemePicker(): void {
    this.publish(closeThemePicker(this.state));
  }
  submitChat(value: string): Promise<void> {
    const text = value.trim();
    if (!text.startsWith('/')) return this.sendChat(value);
    return this.submit(text);
  }

  sendChat(value: string): Promise<void> {
    this.chatSubmissions.push(value);
    this.state = {
      ...this.state,
      chatConversation: [
        ...this.state.chatConversation,
        {id: 'chat-user', kind: 'user', label: 'You', content: value},
        {
          id: 'chat-analysis',
          kind: 'analysis',
          label: 'Chat analysis',
          content: 'Inspecting configuration events',
        },
        {
          id: 'chat-tool',
          kind: 'tool',
          label: 'Chat tool',
          content: '→ Read(run-events.jsonl)\nFound config_load_failed',
          toolCall: '→ Read(run-events.jsonl)\n',
          toolResponse: 'Found config_load_failed',
        },
        {
          id: 'chat-answer',
          kind: 'assistant',
          label: 'Answer',
          content: 'Recorded diagnostic: agent.toml was not found.',
        },
      ],
    };
    this.#notify();
    return Promise.resolve();
  }
  live(): void {
    this.liveCalls += 1;
    this.state = {...this.state, overlay: null, selectedRound: null, selectedAgentKind: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  selectNextAgent(): void {
    const current = this.state.selectedAgentKind;
    const visibleRound =
      this.state.selectedRound ??
      this.state.rounds.find(round => round.status === 'active')?.number ??
      null;
    const phases = this.state.phases.filter(phase => phase.roundNumber === visibleRound);
    const index = current === null ? -1 : phases.findIndex(phase => phase.kind === current);
    const next = phases[(index + 1 + phases.length) % phases.length];
    this.state = {...this.state, selectedAgentKind: next?.kind ?? null, overlay: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  selectPreviousAgent(): void {
    this.selectNextAgent();
  }
  selectNextRound(): void {
    const index = this.state.rounds.findIndex(round => round.number === this.state.selectedRound);
    const next =
      this.state.rounds[(index + 1 + this.state.rounds.length) % this.state.rounds.length];
    this.state = {...this.state, selectedRound: next?.number ?? null, selectedAgentKind: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  selectPreviousRound(): void {
    this.selectNextRound();
  }
  selectRound(roundNumber: number): void {
    this.state = {...this.state, selectedRound: roundNumber, selectedAgentKind: null};
    for (const listener of this.#listeners) listener(this.state);
  }
  toggleTodos(): void {
    this.state = {...this.state, todosExpanded: !this.state.todosExpanded};
    for (const listener of this.#listeners) listener(this.state);
  }

  /** Rows the fake server returns for query.experiments. */
  experiments: HypothesisEntry[] = [];

  openExperimentLog(): Promise<void> {
    this.publish(setExperiments(openExperimentLog(this.state), this.experiments));
    return Promise.resolve();
  }
  moveExperimentSelection(delta: number): void {
    this.publish(moveExperimentSelection(this.state, delta));
  }
  enterExperimentDrilldown(): void {
    this.publish(enterExperimentDrilldown(this.state));
  }
  openPane(view: PaneView): Promise<void> {
    this.publish(setPaneContent(openPane(this.state, view), view, this.paneContent));
    return Promise.resolve();
  }
  closePane(): void {
    this.publish(closePane(this.state));
  }
  cyclePaneFocus(): void {
    this.publish(cyclePaneFocus(this.state));
  }
  focusPane(focus: PaneFocus): void {
    this.publish(focusPane(this.state, focus));
  }
  setChatDockFits(fits: boolean): void {
    this.publish(setChatDockFits(this.state, fits));
  }

  /** Content the fake server returns for whichever visualization is opened. */
  paneContent = 'Performance · median_tok_per_sec\n  1200 ┤●';

  openRound(roundNumber?: number): void {
    if (roundNumber === undefined) {
      this.enterExperimentDrilldown();
      return;
    }
    const scoped = enterExperimentRound(this.state, roundNumber);
    if (scoped !== null) this.publish(scoped);
  }
  leaveExperimentDrilldown(): void {
    this.publish(leaveExperimentDrilldown(this.state));
  }

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.state);
    return () => this.#listeners.delete(listener);
  }

  #notify(): void {
    for (const listener of this.#listeners) listener(this.state);
  }
}
