import {createTestRenderer} from '@opentui/core/testing';
import {afterEach, describe, expect, it} from 'vitest';
import {createOpenTuiApp, type OpenTuiApp, type SupervisionClientLike} from './app.js';
import type {ProtocolResponse, RequestInput} from './protocol.js';

const cleanup: Array<() => void> = [];

afterEach(() => {
  for (const destroy of cleanup.splice(0).reverse()) destroy();
});

describe('OpenTUI app', () => {
  it('renders live output with a persistent input panel', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 20});
    const app = createOpenTuiApp(testRenderer.renderer, new FakeClient());
    registerCleanup(testRenderer.renderer, app);

    const frame = await testRenderer.waitForFrame(value => value.includes('latest agent output'));
    expect(frame).toContain('running · optimizer · round 2');
    expect(frame).toContain('Ask or command');
    expect(frame).toContain('Type a question or /help');
  });

  it('uses the native scrollbox for long output', async () => {
    const lines = Array.from({length: 50}, (_, index) => `output line ${index + 1}`).join('\n');
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const app = createOpenTuiApp(testRenderer.renderer, new FakeClient(`${lines}\n`));
    registerCleanup(testRenderer.renderer, app);

    await testRenderer.waitForFrame(value => value.includes('output line 50'));
    testRenderer.mockInput.pressKey('HOME');
    const frame = await testRenderer.waitForFrame(value => value.includes('output line 1'));
    expect(frame).not.toContain('output line 50');
  });

  it('exits after the backend reaches a terminal state', async () => {
    const testRenderer = await createTestRenderer({width: 80, height: 16});
    const app = createOpenTuiApp(testRenderer.renderer, new FakeClient('', 'completed'));
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

class FakeClient implements SupervisionClientLike {
  private deliveredOutput = false;

  constructor(
    private readonly output = 'latest agent output\n',
    private readonly status = 'running',
  ) {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    if (input.type === 'query.snapshot') {
      return Promise.resolve(response({
        snapshot: {
          run_id: 'test-run',
          sequence: 1,
          status: this.status,
          agent_kind: 'optimizer',
          round_label: 'round 2',
        },
      }));
    }
    if (input.type === 'query.events' && !this.deliveredOutput) {
      this.deliveredOutput = true;
      return Promise.resolve(response({
        events: [{
          sequence: 1,
          timestamp: '2026-01-01T00:00:00Z',
          type: 'output',
          data: {kind: 'output', stream: 'stdout', content: this.output},
        }],
      }));
    }
    return Promise.resolve(response({events: []}));
  }
}

function response(fields: Partial<ProtocolResponse>): ProtocolResponse {
  return {request_id: 'test-request', ok: true, ...fields};
}
