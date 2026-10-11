import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {targetFromCapability} from '../entities/gateway.js';
import {createDemoApp} from './compose.js';

test('renders an initial capability launch before its exchange settles', () => {
  const target = targetFromCapability(
    'http://127.0.0.1:5173/',
    'http://127.0.0.1:8765/?token=launch-secret',
  );
  if (target.bootstrapUrl === null) throw new Error('Expected a bootstrap target');
  const attempts: Array<{url: string; signal: AbortSignal}> = [];
  const app = createDemoApp(
    {
      page: 'http://127.0.0.1:5173/',
      initialGatewayTarget: {...target, bootstrapUrl: target.bootstrapUrl},
      onGatewayConnected: () => undefined,
    },
    {
      bootstrap: (url, signal) => {
        attempts.push({url, signal});
        return new Promise<string>(() => undefined);
      },
      loadReplay: async () => [],
    },
  );

  const html = renderToStaticMarkup(app);

  expect(attempts).toHaveLength(1);
  expect(attempts[0]).toMatchObject({
    url: 'http://127.0.0.1:8765/?token=launch-secret',
    signal: expect.any(AbortSignal),
  });
  expect(html).toContain('Connecting…');
  expect(html).toContain('>Cancel</button>');
  expect(html).toContain('role="status"');
  expect(html).toContain('aria-live="polite"');
  expect(html).toContain('aria-atomic="true"');
  expect(html).not.toContain('launch-secret');
});
