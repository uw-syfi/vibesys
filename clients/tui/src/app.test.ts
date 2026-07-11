import {createTestRenderer} from '@opentui/core/testing';
import {afterEach, describe, expect, it} from 'vitest';
import {createOpenTuiApp, toolOutputPreview, type OpenTuiApp} from './app.js';
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
      conversation: [{
        id: '1', kind: 'assistant', label: 'optimizer · round 2',
        content: '## Result\n\nUse `fast_path()`.',
      }],
    });
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('fast_path()'));
    expect(frame).toContain('running · optimizer · round 2');
    expect(frame).toContain('Result');
    expect(frame).toContain('Ask or command');
    expect(frame).toContain('Type a question or /help');
  });

  it('uses the native scrollbox for long output', async () => {
    const lines = Array.from({length: 50}, (_, index) => `tool output line ${index + 1}`).join('\n');
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

  it('exits after the model reaches a terminal state', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const controller = new FakeController({...initialSessionState(), terminal: true});
    const app = createOpenTuiApp(testRenderer.renderer, controller);
    cleanup.push(() => app.destroy());

    await new Promise<void>(resolve => testRenderer.renderer.once('destroy', resolve));
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

  constructor(public state: SessionState) {}

  start(): Promise<void> { return Promise.resolve(); }
  stop(): Promise<void> { return Promise.resolve(); }
  submit(): Promise<void> { return Promise.resolve(); }
  live(): void {}

  subscribe(listener: (state: SessionState) => void): () => void {
    this.#listeners.add(listener);
    listener(this.state);
    return () => this.#listeners.delete(listener);
  }
}
