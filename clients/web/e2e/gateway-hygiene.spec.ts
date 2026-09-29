import {expect, test} from '@playwright/test';
import {startLiveGateway} from './gateway.js';

/** Loopback literals: 127.0.0.0/8, the IPv6 loopback, and the reserved name. */
const LOOPBACK_HOST = /^(?:127(?:\.\d{1,3}){3}|\[::1\]|localhost)$/;

const EXPECTED_POLICY: Readonly<Record<string, string>> = {
  'default-src': "'none'",
  'script-src': "'self'",
  'style-src': "'self'",
  'img-src': "'self'",
  'font-src': "'none'",
  'connect-src': "'self' ws://127.0.0.1:*",
  'base-uri': "'none'",
  'form-action': "'none'",
  'frame-ancestors': "'none'",
  'object-src': "'none'",
};

declare global {
  interface Window {
    vibesysCspViolations?: string[];
  }
}

test('serves the app under a strict CSP without leaving loopback', async ({page}) => {
  const gateway = startLiveGateway();
  try {
    await page.addInitScript(() => {
      const violations: string[] = [];
      window.vibesysCspViolations = violations;
      document.addEventListener('securitypolicyviolation', event => {
        violations.push(`${event.effectiveDirective} blocked ${event.blockedURI}`);
      });
    });

    const requested: string[] = [];
    const offLoopback: string[] = [];
    await page.route('**/*', async route => {
      const url = route.request().url();
      requested.push(url);
      if (isLoopback(url)) {
        await route.continue();
        return;
      }
      offLoopback.push(url);
      await route.abort('blockedbyclient');
    });

    const sockets: string[] = [];
    page.on('websocket', socket => sockets.push(socket.url()));
    const consoleErrors: string[] = [];
    page.on('console', message => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });

    const response = await page.goto(gateway.url);
    await expect(page.getByRole('heading', {name: 'round-2'})).toBeVisible();
    await expect(page.getByText('15 folded events')).toBeVisible();
    await expect.poll(() => sockets.length).toBeGreaterThan(0);

    // The bundle is self-contained: no CDN script, stylesheet, or font.
    expect(offLoopback).toEqual([]);
    expect(sockets.filter(url => !isLoopback(url))).toEqual([]);
    // Guard against a vacuous pass where interception never saw the subresources.
    expect(requested.filter(url => new URL(url).pathname.endsWith('.js'))).not.toEqual([]);
    expect(requested.filter(url => new URL(url).pathname.endsWith('.css'))).not.toEqual([]);

    const headers = response?.headers() ?? {};
    expect(policyDirectives(headers['content-security-policy'] ?? '')).toEqual(EXPECTED_POLICY);
    expect(headers['referrer-policy']).toBe('no-referrer');

    // A violation fails here instead of only reaching the console.
    const violations = await page.evaluate(
      () => window.vibesysCspViolations ?? ['the violation listener was not installed'],
    );
    expect(violations).toEqual([]);
    expect(consoleErrors.filter(text => text.includes('Content Security Policy'))).toEqual([]);
  } finally {
    gateway.stop();
  }
});

function isLoopback(url: string): boolean {
  const parsed = new URL(url);
  if (parsed.protocol === 'data:' || parsed.protocol === 'blob:') return true;
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
