import {afterEach, describe, expect, it} from 'bun:test';
import {InputRenderable, rgbToHex} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {SessionController} from '../session-controller.js';
import {
  closeThemePicker,
  initialSessionState,
  moveThemeSelection,
  type SessionState,
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

    await testRenderer.mockInput.typeText('/hi');
    const suggestions = await testRenderer.waitForFrame(value => value.includes('[Tab]'));
    expect(suggestions).toContain('/history');
    expect(suggestions).not.toContain('/help  ');
    expect(suggestions).not.toContain('/perf');
    expect(suggestions.indexOf('/history')).toBeLessThan(suggestions.indexOf('Ask or command'));
    expect(testRenderer.renderer.root.findDescendantById('input-box')?.height).toBe(3);

    testRenderer.mockInput.pressKey('TAB');
    testRenderer.mockInput.pressEnter();
    await testRenderer.waitForFrame(() => controller.submissions.length === 1);
    expect(controller.submissions).toEqual(['/history']);
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
});

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

  constructor(public state: SessionState) {}

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

  selectAgent(kind: string): void {
    this.state = {...this.state, selectedAgentKind: kind, overlay: null};
    for (const listener of this.#listeners) listener(this.state);
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

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.state);
    return () => this.#listeners.delete(listener);
  }

  #notify(): void {
    for (const listener of this.#listeners) listener(this.state);
  }
}
