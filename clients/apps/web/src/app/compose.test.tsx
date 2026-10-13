import {expect, test} from 'bun:test';
import {targetFromCapability} from '@vibesys/feature-gateway-connect/renderer';
import {renderToStaticMarkup} from 'react-dom/server';
import {createDemoApp, createLiveCampaignApp} from './compose.js';

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
      loadCampaign: () => new Promise(() => undefined),
    },
  );

  const html = renderToStaticMarkup(app.view);

  expect(attempts).toHaveLength(1);
  expect(attempts[0]?.url).toBe('http://127.0.0.1:8765/?token=launch-secret');
  expect(attempts[0]?.signal).toBeInstanceOf(AbortSignal);
  expect(html).toContain('Connecting…');
  expect(html).toContain('>Cancel</button>');
  expect(html).toContain('role="status"');
  expect(html).toContain('aria-live="polite"');
  expect(html).toContain('aria-atomic="true"');
  expect(html).not.toContain('launch-secret');
  app.close();
  expect(attempts[0]?.signal.aborted).toBe(true);
});

test('the page owner releases a preview stream independently of React render lifetimes', () => {
  let active = false;
  const page = createLiveCampaignApp({
    subscribe: () => {
      active = true;
      return () => {
        active = false;
      };
    },
  });
  expect(active).toBe(true);
  renderToStaticMarkup(page.view);
  expect(active).toBe(true);
  page.close();
  expect(active).toBe(false);
});
