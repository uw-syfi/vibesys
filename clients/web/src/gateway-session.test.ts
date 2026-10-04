import {describe, expect, test} from 'bun:test';
import {
  bootstrapGateway,
  browserSessionForGateway,
  GatewaySessionStore,
  resolveGatewayTarget,
  scrubLaunchCapability,
  serializeBrowserSession,
  targetFromCapability,
  webSocketUrlWithSession,
} from './gateway-session.js';

describe('gateway session lifecycle', () => {
  test('turns a direct capability page into clean durable browser state', () => {
    const target = resolveGatewayTarget('http://127.0.0.1:8765/?token=launch-secret', null);

    expect(target).toEqual({
      gatewayUrl: 'http://127.0.0.1:8765/',
      cleanPageUrl: 'http://127.0.0.1:8765/',
      webSocketUrl: 'ws://127.0.0.1:8765/ws',
      bootstrapUrl: null,
    });
  });

  test('keeps a harness launch secret only in the transient bootstrap request', () => {
    const target = resolveGatewayTarget(
      'http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dlaunch-secret',
      null,
    );

    expect(target?.bootstrapUrl).toBe('http://127.0.0.1:8765/?token=launch-secret');
    expect(target?.cleanPageUrl).toBe(
      'http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F',
    );
    expect(target?.webSocketUrl).toBe('ws://127.0.0.1:8765/ws');
    expect(JSON.stringify(target).includes('launch-secret')).toBe(true);
    expect(`${target?.cleanPageUrl}${target?.webSocketUrl}`).not.toContain('launch-secret');
  });

  test('scrubs an invalid nested capability before validation can reject it', () => {
    const launch =
      'https://viewer.test/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dlaunch-secret';

    expect(() => resolveGatewayTarget(launch, null)).toThrow(
      'An HTTPS browser page requires an HTTPS gateway URL',
    );
    expect(scrubLaunchCapability(launch)).toBe(
      'https://viewer.test/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F',
    );
    expect(
      scrubLaunchCapability(
        'https://viewer.test/?gateway=http%3A%2F%2F%5B%3A%3A1%2F%3Ftoken%3Dsecret',
      ),
    ).toBe('https://viewer.test/');
  });

  test('treats denied session storage as optional browser state', () => {
    const denied = new GatewaySessionStore(() => {
      throw new DOMException('storage denied', 'SecurityError');
    });

    expect(denied.rememberedGateway()).toBeNull();
    expect(denied.browserSession('http://127.0.0.1:8765/')).toBeNull();
    expect(() => denied.rememberGateway('http://127.0.0.1:8765/')).not.toThrow();
    expect(() =>
      denied.rememberBrowserSession('http://127.0.0.1:8765/', 'browser-session'),
    ).not.toThrow();
    const target = resolveGatewayTarget('http://127.0.0.1:8765/?token=launch-secret', null);
    expect(target?.webSocketUrl).toBe('ws://127.0.0.1:8765/ws');
  });

  test('restores only a direct same-origin target from nonsecret session state', () => {
    expect(resolveGatewayTarget('http://127.0.0.1:8765/', 'http://127.0.0.1:8765/')).not.toBeNull();
    expect(resolveGatewayTarget('http://127.0.0.1:5173/', 'http://127.0.0.1:8765/')).toBeNull();
    expect(resolveGatewayTarget('http://127.0.0.1:8765/', 'not a URL')).toBeNull();
  });

  test('validates pasted capabilities before retaining their authority', () => {
    expect(() =>
      targetFromCapability('https://viewer.test/', 'http://127.0.0.1:8765/?token=x'),
    ).toThrow('An HTTPS browser page requires an HTTPS gateway URL');
    expect(() => targetFromCapability('http://127.0.0.1:5173/', 'file:///tmp/run')).toThrow(
      'Use an http:// or https:// gateway URL',
    );
    expect(() => targetFromCapability('http://127.0.0.1:5173/', 'http://127.0.0.1:8765/')).toThrow(
      'must include its capability token',
    );
  });

  test('uses a credentialed no-store bootstrap and rejects a failed exchange', async () => {
    const calls: Array<{url: string; init: RequestInit | undefined}> = [];
    const successful = async (
      url: string | URL | Request,
      init?: RequestInit,
    ): Promise<Response> => {
      calls.push({url: String(url), init});
      return new Response('', {
        status: 200,
        headers: {'X-VibeSys-Browser-Session': 'browser-session'},
      });
    };

    expect(await bootstrapGateway('http://127.0.0.1:8765/?token=secret', successful)).toBe(
      'browser-session',
    );
    expect(calls).toEqual([
      {
        url: 'http://127.0.0.1:8765/?token=secret',
        init: {cache: 'no-store', credentials: 'include', mode: 'cors'},
      },
    ]);

    const rejected = async (): Promise<Response> => new Response('', {status: 403});
    await expect(bootstrapGateway('http://127.0.0.1:8765/?token=old', rejected)).rejects.toThrow(
      'HTTP 403',
    );
  });

  test('scopes the exchanged browser session to its gateway', () => {
    const stored = serializeBrowserSession('http://127.0.0.1:8765/', 'browser-session');
    expect(browserSessionForGateway(stored, 'http://127.0.0.1:8765/')).toBe('browser-session');
    expect(browserSessionForGateway(stored, 'http://127.0.0.1:9000/')).toBeNull();
    expect(browserSessionForGateway('{"version":1,"token":"x","extra":true}', 'x')).toBeNull();
    expect(webSocketUrlWithSession('ws://127.0.0.1:8765/ws', 'browser-session')).toBe(
      'ws://127.0.0.1:8765/ws?session=browser-session',
    );
  });
});
