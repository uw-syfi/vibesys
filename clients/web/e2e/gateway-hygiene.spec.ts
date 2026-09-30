import {expect, type Page, test} from '@playwright/test';
import {startLiveGateway} from './gateway.js';

// The one wall-clock value in this file, and a resource bound rather than the
// flakiness patch `.agents/skills/testing/references/flakiness.md` prohibits.
// It does not make a racing assertion pass: every asynchronous condition below
// is an `expect`/`expect.poll` on an observable state, none of which waits on
// elapsed time. What the budget covers is process startup the spec cannot
// avoid: a cold `uv run` resolving the Python environment, plus
// `entrypoints.server`'s own 10s readiness budget, running concurrently with
// the other gateway-booting spec and the Vite dev server. That cost is bounded
// and independent of what is asserted, and it does not fit the 30s global
// default. Scoped to this file; the global config is left alone.
test.describe.configure({timeout: 120_000});

/**
 * Loopback literals a URL may use: any 127.0.0.0/8 form including the dotted
 * shorthands, the IPv6 loopback, and the reserved name.
 */
const LOOPBACK_HOST = /^(?:127(?:\.\d{1,3}){0,3}|\[::1\]|localhost)$/;
const NETWORK_PROTOCOLS = new Set(['http:', 'https:', 'ws:', 'wss:']);

/** Documentation-range host with no route off this machine. */
const OFF_ORIGIN_IMAGE = 'http://198.51.100.7/negative-control.png';

type CspWindow = Window & {vibesysCspViolations?: string[]};

interface Intercepted {
  readonly requested: string[];
  readonly offLoopback: string[];
}

test('serves the app under a strict CSP without leaving loopback', async ({page}) => {
  const gateway = startLiveGateway();
  try {
    await installViolationListener(page);
    const http = await interceptHttp(page);
    const sockets = await interceptWebSockets(page);
    const consoleErrors: string[] = [];
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });

    const response = await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
    await expect(page.getByText('15 folded events')).toBeVisible();
    await expect.poll(() => sockets.requested.length).toBeGreaterThan(0);

    // The bundle is self-contained: no CDN script, stylesheet, or font, and no
    // socket to anything but this gateway.
    expect(http.offLoopback).toEqual([]);
    expect(sockets.offLoopback).toEqual([]);
    // Guard against a vacuous pass where interception never saw the page's
    // subresources. Asserted as "the gateway served the bundle" rather than as
    // a chunk shape: pinning `.css` would pin Vite's chunking, so inlining the
    // stylesheet would turn a security property red for a build-config change
    // that is not a gateway regression.
    expect(http.requested.filter(url => new URL(url).pathname.startsWith('/assets/'))).not.toEqual(
      [],
    );

    const headers = response?.headers() ?? {};
    expect(policyDirectives(headers['content-security-policy'] ?? '')).toEqual(
      expectedPolicy(gateway.url),
    );
    expect(headers['referrer-policy']).toBe('no-referrer');
    expect(headers['x-content-type-options']).toBe('nosniff');

    // A violation fails here instead of only reaching the console.
    expect(await readViolations(page)).toEqual([]);
    expect(consoleErrors.filter(text => text.includes('Content Security Policy'))).toEqual([]);

    await assertTheListenerReportsAViolation(page, http);

    // A detached gateway that outlived the spec would be silent otherwise.
    // The pid is the load-bearing half: `_run_stop` in `src/entrypoints/web.py`
    // currently returns 0 unconditionally, including when its wait loop times
    // out with the child still alive, so the exit status only becomes
    // meaningful once #1028 lands.
    const stopped = gateway.stop();
    await expect.poll(() => processIsGone(gateway.pid)).toBe(true);
    expect(stopped.status).toBe(0);
  } finally {
    // Read in `finally`, not in the body: `stop()` memoizes, so this is the
    // same result the body saw, and annotating here reports teardown errors
    // even when a body assertion failed first.
    const stopped = gateway.stop();
    if (stopped.errors.length > 0) {
      test.info().annotations.push({type: 'teardown', description: stopped.errors.join('; ')});
    }
  }
});

/** True once the detached gateway process no longer exists. */
function processIsGone(pid: number): boolean {
  try {
    // Signal 0 only probes for existence; it delivers nothing.
    process.kill(pid, 0);
    return false;
  } catch {
    return true;
  }
}

/**
 * Negative control for the assertion above.
 *
 * The `securitypolicyviolation` listener is the load-bearing half of the audit,
 * and "no violations recorded" cannot be told apart from "violations never
 * reached the listener" unless something violates the policy on purpose. An
 * off-origin image is denied by `img-src 'self'`, and the CSP denies it before
 * the network layer, so the request is never made either.
 */
async function assertTheListenerReportsAViolation(page: Page, http: Intercepted): Promise<void> {
  await page.evaluate(async url => {
    await new Promise<void>(resolve => {
      const image = document.createElement('img');
      image.addEventListener('error', () => resolve());
      image.addEventListener('load', () => resolve());
      image.src = url;
      document.body.append(image);
    });
  }, OFF_ORIGIN_IMAGE);

  await expect.poll(async () => (await readViolations(page)).length).toBeGreaterThan(0);
  const violations = await readViolations(page);
  expect(violations.filter(entry => entry.startsWith('img-src'))).not.toEqual([]);
  expect(http.requested).not.toContain(OFF_ORIGIN_IMAGE);
  expect(http.offLoopback).toEqual([]);
}

async function installViolationListener(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const violations: string[] = [];
    (window as CspWindow).vibesysCspViolations = violations;
    document.addEventListener('securitypolicyviolation', event => {
      violations.push(`${event.effectiveDirective} blocked ${event.blockedURI}`);
    });
  });
}

async function readViolations(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as CspWindow).vibesysCspViolations ?? ['listener missing']);
}

async function interceptHttp(page: Page): Promise<Intercepted> {
  const intercepted: Intercepted = {requested: [], offLoopback: []};
  await page.route('**/*', async route => {
    const url = route.request().url();
    intercepted.requested.push(url);
    if (isLoopback(url)) {
      await route.continue();
      return;
    }
    intercepted.offLoopback.push(url);
    await route.abort('blockedbyclient');
  });
  return intercepted;
}

/**
 * Restrict the socket side too.
 *
 * `page.route` does not see WebSocket handshakes, so observing `page.on
 * ('websocket')` after the fact would only report an escape, not prevent one.
 * Routing the socket makes the loopback rule a restriction on the app's own
 * code: a loopback URL is connected straight through to the real server with
 * frames forwarded both ways, and anything else is closed by the route
 * handler. This is a renderer-side `globalThis.WebSocket` override, not a
 * network-layer block, so a socket opened from a Worker or through a
 * previously captured native constructor would bypass it. Both are things the
 * app does not do, and the `connect-src` assertion is what covers them.
 */
async function interceptWebSockets(page: Page): Promise<Intercepted> {
  const intercepted: Intercepted = {requested: [], offLoopback: []};
  await page.routeWebSocket('**/*', route => {
    const url = route.url();
    intercepted.requested.push(url);
    if (!isLoopback(url)) {
      intercepted.offLoopback.push(url);
      route.close({code: 1008, reason: 'blocked by the loopback audit'});
      return;
    }
    route.connectToServer();
  });
  return intercepted;
}

/**
 * Independent cross-language restatement of `_POLICY_DIRECTIVES` in
 * `src/server/transport/websocket.py`. It imports nothing from the gateway, so
 * it fails when the two disagree rather than agreeing by construction. The
 * gateway's socket sources are always its own port, one per accepted
 * hostname; this spec declares no extra browser origin, so the page's own
 * authority is the whole list.
 */
function expectedPolicy(pageUrl: string): Readonly<Record<string, string>> {
  return {
    'default-src': "'none'",
    'script-src': "'self'",
    'style-src': "'self'",
    'img-src': "'self'",
    'font-src': "'none'",
    'connect-src': `'self' ws://${new URL(pageUrl).host}`,
    'base-uri': "'none'",
    'form-action': "'none'",
    'frame-ancestors': "'none'",
    'object-src': "'none'",
  };
}

function isLoopback(url: string): boolean {
  const parsed = new URL(url);
  if (!NETWORK_PROTOCOLS.has(parsed.protocol)) return false;
  return LOOPBACK_HOST.test(parsed.hostname);
}

function policyDirectives(policy: string): Record<string, string> {
  const directives: Record<string, string> = {};
  for (const directive of policy.split(';')) {
    const [name, ...sources] = directive.trim().split(/\s+/);
    if (name === undefined || name === '') continue;
    directives[name] = sources.join(' ');
  }
  return directives;
}
