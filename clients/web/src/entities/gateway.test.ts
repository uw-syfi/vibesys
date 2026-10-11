import {describe, expect, test} from 'bun:test';
import {
  browserSessionForGateway,
  resolveGatewayTarget,
  scrubLaunchCapability,
  serializeBrowserSession,
  targetFromCapability,
  webSocketUrlWithSession,
} from './gateway.js';

describe('gateway session lifecycle', () => {
  test('turns a direct capability page into clean durable browser state', () => {
    const launch = 'http://127.0.0.1:8765/?token=launch-secret';
    const scrubbed = scrubLaunchCapability(launch);
    const target = resolveGatewayTarget(launch, null);

    expect(target).toEqual({
      gatewayUrl: 'http://127.0.0.1:8765/',
      cleanPageUrl: 'http://127.0.0.1:8765/',
      webSocketUrl: 'ws://127.0.0.1:8765/ws',
      bootstrapUrl: null,
    });
    expect(scrubbed).toEqual({
      pageUrl: 'http://127.0.0.1:8765/',
      historyState: {
        vibesysGateway: {
          kind: 'direct-gateway',
          version: 1,
          gatewayUrl: 'http://127.0.0.1:8765/',
        },
      },
    });
    expect(JSON.stringify(scrubbed.historyState)).not.toContain('launch-secret');
    expect(resolveGatewayTarget(scrubbed.pageUrl, null, scrubbed.historyState)).toEqual(target);
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
    expect(scrubLaunchCapability(launch)).toEqual({
      pageUrl: 'https://viewer.test/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F',
      historyState: null,
    });
    expect(
      scrubLaunchCapability(
        'https://viewer.test/?gateway=http%3A%2F%2F%5B%3A%3A1%2F%3Ftoken%3Dsecret',
      ),
    ).toEqual({pageUrl: 'https://viewer.test/', historyState: null});
    expect(scrubLaunchCapability('file:///viewer.html?token=secret')).toEqual({
      pageUrl: 'file:///viewer.html',
      historyState: null,
    });
  });

  test('accepts only exact same-origin direct-gateway history provenance', () => {
    const page = 'http://127.0.0.1:8765/';
    const valid = scrubLaunchCapability(`${page}?token=secret`).historyState;
    expect(resolveGatewayTarget(page, null, valid)).not.toBeNull();

    const invalid = [
      null,
      {},
      {vibesysGateway: {kind: 'direct-gateway', version: 2, gatewayUrl: page}},
      {vibesysGateway: {kind: 'direct-gateway', version: 1, gatewayUrl: page}, extra: true},
      {
        vibesysGateway: {
          kind: 'direct-gateway',
          version: 1,
          gatewayUrl: `${page}?token=secret`,
        },
      },
      {
        vibesysGateway: {
          kind: 'direct-gateway',
          version: 1,
          gatewayUrl: 'http://127.0.0.1:9000/',
        },
      },
      {
        vibesysGateway: {kind: 'direct-gateway', version: 1, gatewayUrl: 'file:///viewer.html'},
      },
    ];
    for (const historyState of invalid) {
      expect(resolveGatewayTarget(page, null, historyState)).toBeNull();
    }
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
