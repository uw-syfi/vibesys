import {describe, expect, test} from 'bun:test';
import {isAllowedRequest, isAppUrl, originOf, parseLaunchUrl} from './launch-url.js';

const TOKEN = 'capability-Secret_123';
const PORTS = [1, 5173, 8765, 51234, 65535];

describe('parseLaunchUrl', () => {
  test('accepts every loopback gateway capability URL and derives its origin', () => {
    for (const port of PORTS) {
      const target = parseLaunchUrl(`http://127.0.0.1:${port}/?token=${TOKEN}`);
      expect(target.origin).toBe(`http://127.0.0.1:${port}`);
      expect(new URL(target.url).searchParams.get('token')).toBe(TOKEN);
    }
  });

  test('rejects every URL outside the gateway contract without quoting the token', () => {
    const rejected = [
      undefined,
      '',
      'not a url',
      `https://127.0.0.1:8765/?token=${TOKEN}`,
      `http://localhost:8765/?token=${TOKEN}`,
      `http://0.0.0.0:8765/?token=${TOKEN}`,
      `http://example.com:8765/?token=${TOKEN}`,
      'http://127.0.0.1:8765/',
      'http://127.0.0.1:8765/?token=',
      `file:///tmp/index.html?token=${TOKEN}`,
    ];
    for (const raw of rejected) {
      expect(() => parseLaunchUrl(raw)).toThrow(/VIBESYS_DESKTOP_URL/);
      try {
        parseLaunchUrl(raw);
      } catch (error) {
        expect((error as Error).message).not.toContain(TOKEN);
      }
    }
  });
});

describe('request confinement', () => {
  const origin = 'http://127.0.0.1:8765';

  test('allows the gateway origin over HTTP and WebSocket, at any path', () => {
    for (const path of ['/', '/assets/index.js', '/ws', `/?token=${TOKEN}`]) {
      expect(isAppUrl(`${origin}${path}`, origin)).toBe(true);
      expect(isAllowedRequest(`${origin}${path}`, origin)).toBe(true);
      expect(isAllowedRequest(`ws://127.0.0.1:8765${path}`, origin)).toBe(true);
    }
  });

  test('blocks any other scheme, host or port', () => {
    const others = [
      'http://127.0.0.1:8766/',
      'https://127.0.0.1:8765/',
      'http://localhost:8765/',
      'ws://127.0.0.1:8766/ws',
      'wss://127.0.0.1:8765/ws',
      'ws://localhost:8765/ws',
      'https://example.com/',
      'not a url',
    ];
    for (const url of others) {
      expect(isAppUrl(url, origin)).toBe(false);
      expect(isAllowedRequest(url, origin)).toBe(false);
    }
  });

  test('originOf never returns the query', () => {
    expect(originOf(`${origin}/?token=${TOKEN}`)).toBe(origin);
    expect(originOf('not a url')).not.toContain('not a url');
  });
});
