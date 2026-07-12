import {createTestRenderer} from '@opentui/core/testing';
import {afterEach, describe, expect, it} from 'vitest';
import {createOpenTuiApp, type OpenTuiApp, promptPreview, toolOutputPreview} from './app.js';
import type {SessionController} from './session-controller.js';
import {initialSessionState, type SessionState} from './session-model.js';

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
      conversation: [
        {
          id: '1',
          kind: 'assistant',
          label: 'optimizer · round 2',
          content: '## Result\n\nUse `fast_path()`.',
        },
      ],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('fast_path()'));
    expect(frame).toContain('running · optimizer · round 2');
    expect(frame).toContain('Result');
    expect(frame).toContain('Ask or command');
    expect(frame).toContain('Type a question or /help');
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

  it('advertises Escape and returns a non-live view to live output', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      view: 'help',
      detailContent: 'Available commands',
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('Esc: back to live'));
    testRenderer.mockInput.pressKey('ESCAPE');
    await testRenderer.waitForFrame(value => !value.includes('Esc: back to live'));
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

  it('limits tool output without discarding the underlying content', () => {
    const content = Array.from({length: 20}, (_, index) => `line ${index + 1}`).join('\n');
    const preview = toolOutputPreview(content);

    expect(preview).toContain('line 12');
    expect(preview).not.toContain('line 13');
    expect(preview).toContain('8 more lines hidden');
    expect(content).toContain('line 20');
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
    expect(frame.match(/╭/g)).toHaveLength(3);
  });

  it('collapses prompts and expands the latest prompt with Ctrl+P', async () => {
    const content = Array.from({length: 20}, (_, index) => `prompt line ${index + 1}`).join('\n');
    expect(promptPreview(content, false)).toMatchObject({hiddenLines: 8});
    expect(promptPreview(content, false).content).not.toContain('prompt line 13');
    expect(promptPreview(content, true).content).toContain('prompt line 20');

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

  it('keeps terminal results visible until the operator exits', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({
      ...initialSessionState(),
      status: 'failed',
      terminal: true,
      view: 'error',
      detailContent: 'Invalid --max-rounds value',
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);
    let destroyed = false;
    testRenderer.renderer.once('destroy', () => {
      destroyed = true;
    });

    await testRenderer.waitForFrame(value => value.includes('Invalid --max-rounds value'));
    await new Promise(resolve => setTimeout(resolve, 150));
    expect(destroyed).toBe(false);
  });
});

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
  liveCalls = 0;

  constructor(public state: SessionState) {}

  start(): Promise<void> {
    return Promise.resolve();
  }
  stop(): Promise<void> {
    return Promise.resolve();
  }
  submit(value: string): Promise<void> {
    this.submissions.push(value);
    return Promise.resolve();
  }
  live(): void {
    this.liveCalls += 1;
    this.state = {...this.state, view: 'live'};
    for (const listener of this.#listeners) listener(this.state);
  }

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.state);
    return () => this.#listeners.delete(listener);
  }
}
