import {expect, test} from 'bun:test';
import {resolveGatewayTarget} from '@vibesys/feature-gateway-connect/renderer';
import {GatewaySessionStore} from './gateway-storage.js';

test('treats denied session storage as optional browser state', () => {
  const denied = new GatewaySessionStore(() => {
    throw new DOMException('storage denied', 'SecurityError');
  });

  expect(denied.remembered()).toEqual({gateway: null, browserSession: null});
  expect(() => denied.rememberGateway('http://127.0.0.1:8765/')).not.toThrow();
  expect(() => denied.rememberBrowserSession('{}')).not.toThrow();
  const target = resolveGatewayTarget('http://127.0.0.1:8765/?token=launch-secret', null);
  expect(target?.webSocketUrl).toBe('ws://127.0.0.1:8765/ws');
});
