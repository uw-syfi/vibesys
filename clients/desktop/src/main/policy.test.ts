import assert from 'node:assert/strict';
import test from 'node:test';
import {
  allowPermission,
  devOrigin,
  homeArguments,
  isAllowedRequest,
  isAppUrl,
  originOf,
  quitRequest,
  windowUrl,
} from './policy.js';

const HOME = 'http://127.0.0.1:8764';
const VITE = 'http://127.0.0.1:5173';

test('the window stays on its exact app origins', () => {
  assert.equal(isAppUrl(`${HOME}/?theme=dark`, [HOME]), true);
  for (const url of [
    'http://localhost:8764/',
    'http://127.0.0.1:8765/',
    'https://127.0.0.1:8764/',
    'https://example.com/',
    'file:///etc/hosts',
    'about:blank',
    'not a url',
  ]) {
    assert.equal(isAppUrl(url, [HOME]), false, url);
  }
  assert.equal(isAppUrl(`${HOME}/`, [VITE]), false);
});

test('requests reach the app origins over HTTP and run gateways over loopback ws only', () => {
  assert.equal(isAllowedRequest(`${HOME}/assets/index.js`, [HOME]), true);
  assert.equal(isAllowedRequest('ws://127.0.0.1:50123/ws?token=t', [HOME]), true);
  for (const url of [
    'ws://localhost:50123/ws',
    'wss://127.0.0.1:50123/ws',
    'http://127.0.0.1:50123/health',
    'https://fonts.googleapis.com/css',
    `${VITE}/src/main.tsx`,
    'not a url',
  ]) {
    assert.equal(isAllowedRequest(url, [HOME]), false, url);
  }
});

test('diagnostics name the origin, never the path or query', () => {
  assert.equal(originOf('https://example.com/a?token=secret'), 'https://example.com');
  assert.equal(originOf('::'), 'an unparseable URL');
});

test('only clipboard writes are permitted', () => {
  assert.equal(allowPermission('clipboard-sanitized-write'), true);
  for (const permission of ['media', 'notifications', 'geolocation', 'openExternal']) {
    assert.equal(allowPermission(permission), false, permission);
  }
});

test('prod passes only an explicit port; dev pins the proxy port and adds the Vite origin', () => {
  assert.deepEqual(homeArguments(undefined, null), []);
  assert.deepEqual(homeArguments('8799', null), ['--port', '8799']);
  assert.deepEqual(homeArguments(undefined, VITE), ['--port', '8764', '--dev-origin', VITE]);
  assert.deepEqual(homeArguments('8799', VITE), ['--port', '8799', '--dev-origin', VITE]);
  assert.deepEqual(homeArguments('', null), []);
  assert.deepEqual(homeArguments('', VITE), ['--port', '8764', '--dev-origin', VITE]);
});

test('the window opens the app on the mode origin with the home capability', () => {
  const home = {origin: HOME, token: 'abc_-9'};
  assert.equal(windowUrl(home, null), `${HOME}/?token=abc_-9`);
  assert.equal(windowUrl(home, VITE), `${VITE}/?token=abc_-9`);
});

test('dev refuses a reused home outside the exact Vite proxy origin before exposing its capability', () => {
  const home = {origin: 'http://127.0.0.1:9123', token: 'private-capability'};
  for (const port of [undefined, '', '8764', '9124']) {
    assert.throws(
      () => windowUrl(home, VITE, port),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /does not match the Vite API proxy/);
        assert.match(error.message, /VIBESYS_HOME_PORT/);
        assert.ok(!error.message.includes(home.token));
        return true;
      },
    );
  }
  assert.equal(windowUrl(home, VITE, '9123'), `${VITE}/?token=private-capability`);
  assert.equal(windowUrl(home, VITE, '09123'), `${VITE}/?token=private-capability`);
  assert.equal(windowUrl(home, null, '8764'), `${home.origin}/?token=private-capability`);
});

test('dev matches an announced home on the default HTTP port after origin normalization', () => {
  const home = {origin: 'http://127.0.0.1:80', token: 'home-capability'};
  assert.equal(windowUrl(home, VITE, '80'), `${VITE}/?token=home-capability`);
});

test('the dev origin is exactly the Vite origin, and only in a dev build', () => {
  assert.equal(devOrigin(VITE, true), VITE);
  assert.equal(devOrigin(VITE, false), null);
  assert.equal(devOrigin(undefined, true), null);
  for (const url of [
    `${VITE}/`,
    'http://localhost:5173',
    'http://127.0.0.1:5174',
    'https://example.com',
  ]) {
    assert.equal(devOrigin(url, true), null, url);
  }
});

test('every quit is held until the home server stop settles; only the first starts it', () => {
  assert.deepEqual(quitRequest('running'), {hold: true, startStop: true});
  assert.deepEqual(quitRequest('stopping'), {hold: true, startStop: false});
  assert.deepEqual(quitRequest('stopped'), {hold: false, startStop: false});
});
