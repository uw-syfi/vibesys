import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import type {AddressInfo} from 'node:net';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {type HomeOptions, HomeStartError, parseAnnouncement, startHome} from './home.js';

const FAKE_HOME = fileURLToPath(new URL('../../test/fake-home.ts', import.meta.url));

function options(
  args: string[],
  stderr: string[] = [],
  overrides: Partial<HomeOptions> = {},
): HomeOptions {
  return {
    command: [process.execPath, FAKE_HOME, ...args],
    cwd: process.cwd(),
    env: process.env,
    readyTimeoutMs: 10_000,
    stopGraceMs: 200,
    onStderr: line => stderr.push(line),
    ...overrides,
  };
}

async function answers(origin: string): Promise<boolean> {
  try {
    return (await fetch(`${origin}/health`)).ok;
  } catch {
    return false;
  }
}

function listeningPort(stderr: string[]): string {
  const port = /listening on port (\d+)/.exec(stderr.join('\n'))?.[1];
  assert.ok(port !== undefined, 'the fake reported its port');
  return port;
}

test('only the loopback capability line is an announcement', () => {
  assert.deepEqual(parseAnnouncement('VibeSys home: http://127.0.0.1:8764/?token=a-b_C9'), {
    origin: 'http://127.0.0.1:8764',
    token: 'a-b_C9',
  });
  assert.equal(parseAnnouncement('VibeSys home: http://localhost:8764/?token=abc'), null);
  assert.equal(parseAnnouncement('VibeSys home: http://127.0.0.1:8764/x?token=abc'), null);
  assert.equal(parseAnnouncement('Resolved 212 packages in 3ms'), null);
});

test('a started home answers until stop; stdout, which carries the token, never reaches the stderr sink', async () => {
  const stderr: string[] = [];
  const home = await startHome(options(['serve'], stderr));
  assert.equal(home.token, 'fake-token');
  assert.equal(await answers(home.origin), true);
  await home.stop();
  await home.stop();
  assert.deepEqual(await home.ended, {kind: 'stopped'});
  assert.equal(await answers(home.origin), false);
  assert.ok(stderr.some(line => line.startsWith('listening on port')));
  assert.ok(stderr.every(line => !line.includes('fake-token')));
});

test('stop kills a server that ignores SIGINT after the grace period', async () => {
  const home = await startHome(options(['stubborn']));
  await home.stop();
  assert.deepEqual(await home.ended, {kind: 'stopped'});
  assert.equal(await answers(home.origin), false);
});

test('a server that dies on its own is a crash, reported with its stderr', async () => {
  const home = await startHome(options(['serve']));
  const pid = Number(await (await fetch(`${home.origin}/pid`)).text());
  process.kill(pid, 'SIGKILL');
  const end = await home.ended;
  assert.equal(end.kind, 'crashed');
  assert.match(
    end.kind === 'crashed' ? end.detail : '',
    /was killed by SIGKILL\nlistening on port \d+/,
  );
});

test('a launcher that hands over to a running home server is reused, and stop leaves that server alone', async () => {
  const running = createServer((_request, response) => response.end('vibesys-ok\n'));
  await new Promise<void>(resolve => running.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${(running.address() as AddressInfo).port}`;
  try {
    const home = await startHome(options(['announce', `${origin}/?token=theirs`]));
    assert.deepEqual({origin: home.origin, token: home.token}, {origin, token: 'theirs'});
    assert.deepEqual(await home.ended, {kind: 'reused'});
    await home.stop();
    assert.equal(await answers(origin), true);
  } finally {
    running.closeAllConnections();
    running.close();
  }
});

test('a server that exits before announcing fails the start with its stderr tail', async () => {
  await assert.rejects(startHome(options(['fail'])), (error: unknown) => {
    assert.ok(error instanceof HomeStartError);
    assert.match(
      error.message,
      /exited with code 3 before it was ready\nTraceback[\s\S]*Address already in use/,
    );
    return true;
  });
});

test('a server that never announces is killed at the ready timeout', async () => {
  const stderr: string[] = [];
  await assert.rejects(
    startHome(options(['silent'], stderr, {readyTimeoutMs: 500})),
    /not ready within 0.5 s/,
  );
  assert.equal(await answers(`http://127.0.0.1:${listeningPort(stderr)}`), false);
});

test('an announced server that fails its health check is killed and the start fails', async () => {
  const stderr: string[] = [];
  await assert.rejects(startHome(options(['unhealthy'], stderr)), /did not answer \/health/);
  assert.equal(await answers(`http://127.0.0.1:${listeningPort(stderr)}`), false);
});

test('a missing executable fails the start with the command name', async () => {
  await assert.rejects(
    startHome(options([], [], {command: ['vibesys-no-such-home']})),
    /cannot run vibesys-no-such-home/,
  );
});
