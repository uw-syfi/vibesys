import {render} from 'ink-testing-library';
import {afterEach, describe, expect, it} from 'vitest';
import {App, type SupervisionClientLike} from './app.js';
import type {ProtocolResponse, RequestInput} from './protocol.js';

const renderedApps: Array<ReturnType<typeof render>> = [];

afterEach(() => {
  for (const app of renderedApps) app.unmount();
  renderedApps.length = 0;
});

describe('App', () => {
  it('renders streamed output while keeping the input prompt visible', async () => {
    const client = new FakeClient();
    const app = render(<App client={client}/>);
    renderedApps.push(app);

    await expect.poll(() => app.lastFrame()).toContain('latest agent output');
    expect(app.lastFrame()).toContain('running · optimizer · round 2');
    expect(app.lastFrame()).toContain('›');
  });

  it('keeps only the latest output within the terminal viewport', async () => {
    const lines = Array.from({length: 50}, (_, index) => `output line ${index + 1}`).join('\n');
    const client = new FakeClient(`${lines}\n`);
    const app = render(<App client={client}/>);
    renderedApps.push(app);

    await expect.poll(() => app.lastFrame()).toContain('output line 50');
    expect(app.lastFrame()).not.toContain('output line 1\n');
    expect(app.lastFrame()).toContain('›');
  });
});

class FakeClient implements SupervisionClientLike {
  private deliveredOutput = false;

  constructor(private readonly output = 'latest agent output\n') {}

  request(input: RequestInput): Promise<ProtocolResponse> {
    if (input.type === 'query.snapshot') {
      return Promise.resolve(response({
        snapshot: {
          run_id: 'test-run',
          sequence: 1,
          status: 'running',
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
  return {
    request_id: 'test-request',
    ok: true,
    ...fields,
  };
}
