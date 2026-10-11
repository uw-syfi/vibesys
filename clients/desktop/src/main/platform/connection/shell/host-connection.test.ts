import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readdirSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {FakeClock} from '@vibesys/backend-client/testing';
import type {ConnectionConfig} from '../state/host-connection.js';
import {ECHO_SERVER, FakeHelper, FakeLink, record} from '../testing/fake-helper.js';
import {hostConnectionContract, phaseReached} from '../testing/host-connection-contract.js';
import {ConnectionManager} from './connection-manager.js';
import {ConnectionError} from './helper-channel.js';
import {HostConnection} from './host-connection.js';
import {LocalLink, nodeSpawn} from './link.js';

const CONFIG: ConnectionConfig = {delaysMs: [500, 1_000], deadlineMs: 60_000, heartbeatMs: 15_000};
const REPOSITORY = join(import.meta.dir, '../../../../../../..');
const realContracts = process.env['VIBESYS_REAL_CONTRACTS'] === '1';

function fakeConnection(link = new FakeLink(), clock = new FakeClock()): HostConnection {
  return new HostConnection({
    link,
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
  });
}

hostConnectionContract('fake', {
  working: () => fakeConnection(),
  broken: () => {
    const link = new FakeLink();
    link.helper.bootstrapExit = {code: 65, stdout: 'nodir'};
    return fakeConnection(link);
  },
});

/** The real helper of this checkout, on This Mac, with a registry of its own. */
function realConnection(checkout: string): HostConnection {
  const runtime = mkdtempSync(join(tmpdir(), 'vsd-contract-'));
  const link = new LocalLink(checkout, (command, args, env) =>
    nodeSpawn(command, args, {...env, XDG_RUNTIME_DIR: runtime}),
  );
  return new HostConnection({
    link,
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
}

hostConnectionContract(
  'local helper',
  {
    working: () => realConnection(REPOSITORY),
    broken: () => realConnection('/nonexistent-vibesys-contract'),
  },
  !realContracts,
);

describe('HostConnection over the FakeHelper', () => {
  test('a guarded request withdrawn while readiness waits is never written to the helper', async () => {
    const link = new FakeLink();
    link.helper.publish(record('000000000001', 'run-1'));
    const hello = link.helper.deferNext('hello');
    await using connection = fakeConnection(link);
    let current = true;

    const stopping = connection.requestIfCurrent(
      {type: 'run.stop', instance_id: '000000000001', force: false},
      () => current,
    );
    await hello.received;
    current = false;
    hello.release();

    expect(await stopping).toEqual({kind: 'not-written'});
    expect(link.helper.stops).toEqual([]);
    expect(link.helper.host.instances.map(instance => instance.id)).toEqual(['000000000001']);
  });

  test('a guarded request distinguishes a failure after its write from withdrawal', async () => {
    const link = new FakeLink();
    link.helper.publish(record('000000000001', 'run-1'));
    const stopped = link.helper.deferNext('run.stop');
    await using connection = fakeConnection(link);
    await connection.ready();
    let current = true;

    const stopping = connection.requestIfCurrent(
      {type: 'run.stop', instance_id: '000000000001', force: false},
      () => current,
    );
    await stopped.received;
    current = false;
    link.helper.drop();

    const result = await stopping;
    expect(result.kind).toBe('failed');
    expect(result).toMatchObject({written: true});
    expect(link.helper.stops).toEqual([{instanceId: '000000000001', force: false}]);
  });

  test('a dropped link is checked, restored, and the registry watch resumes', async () => {
    const link = new FakeLink();
    await using connection = fakeConnection(link);
    await connection.ready();
    const spawnedBefore = link.helper.spawned;
    link.helper.drop();
    await phaseReached(connection, 'reconnecting');
    await phaseReached(connection, 'ready');
    expect(link.helper.spawned).toBeGreaterThan(spawnedBefore);
    const pushed = new Promise<string[]>(resolve => {
      connection.watchInstances(listing => {
        if (listing.instances.length > 0) resolve(listing.instances.map(entry => entry.id));
      });
    });
    link.helper.publish(record('00000000000a', 'run-a'));
    expect(await pushed).toEqual(['00000000000a']);
  });

  test('a refused sign-in stops the loop until the user retries', async () => {
    const link = new FakeLink();
    link.linkFault = {cause: 'auth', detail: 'Permission denied'};
    await using connection = fakeConnection(link);
    const error = await connection.ready().catch((caught: unknown) => caught as ConnectionError);
    expect(error).toBeInstanceOf(ConnectionError);
    expect(connection.phase.kind).toBe('auth-needed');
    link.linkFault = null;
    connection.dispatch({type: 'user-retry'});
    await phaseReached(connection, 'ready');
    expect(link.interactive).toBe(1);
  });

  test('a run channel relays the run protocol and reports the run id from the record', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000b', 'run-b'));
    link.helper.host.runs.set('00000000000b', ECHO_SERVER);
    await using connection = fakeConnection(link);
    const {runId, stream} = await connection.openRun('00000000000b');
    expect(runId).toBe('run-b');
    const echoed = new Promise<string>(resolve =>
      stream.once('data', (chunk: Buffer) => resolve(chunk.toString())),
    );
    stream.write('{"type":"query.snapshot"}\n');
    expect(JSON.parse(await echoed)).toEqual({echo: '{"type":"query.snapshot"}'});
    stream.destroy();
  });

  test('a link that is down past the schedule goes offline, and a wake starts over', async () => {
    const clock = new FakeClock();
    const link = new FakeLink();
    await using connection = fakeConnection(link, clock);
    await connection.ready();
    link.linkFault = {cause: 'link', detail: 'down'};
    link.helper.drop();
    await phaseReached(connection, 'reconnecting');
    for (let index = 0; index < 10 && connection.phase.kind !== 'offline'; index += 1) {
      await Promise.resolve();
      await new Promise(resolve => setImmediate(resolve));
      clock.advanceBy(10_000);
    }
    expect(connection.phase.kind).toBe('offline');
    link.linkFault = null;
    connection.dispatch({type: 'wake'});
    await phaseReached(connection, 'ready');
  });

  test('closing ends every channel, and leaves the shared link to the manager', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000c', 'run-c'));
    link.helper.host.runs.set('00000000000c', ECHO_SERVER);
    const connection = fakeConnection(link);
    const {stream} = await connection.openRun('00000000000c');
    const closed = new Promise<void>(resolve => stream.once('close', () => resolve()));
    await connection[Symbol.asyncDispose]();
    await connection[Symbol.asyncDispose]();
    await closed;
    expect(link.released).toBe(0);
  });

  test('a checkout check shares the host link, and the manager releases it once, at the end', async () => {
    const link = new FakeLink();
    const manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
      config: CONFIG,
    });
    const kept = manager.connection({kind: 'ssh', alias: 'gpu'}, '/src/vibesys');
    await kept.ready();
    {
      // regression: closing a checkout check ended the ssh master the kept connection used.
      await using candidate = manager.candidate({kind: 'ssh', alias: 'gpu'}, '/src/other');
      await candidate.ready();
    }
    expect(link.released).toBe(0);
    await kept.request({type: 'ping'});
    await manager[Symbol.asyncDispose]();
    expect(link.released).toBe(1);
    expect(kept.phase.kind).toBe('closed');
  });
});

/** The `helper-*` conformance scenarios, replayed against the FakeHelper over its wire. */
describe('FakeHelper conformance', () => {
  const directory = join(REPOSITORY, 'tests/conformance/scenarios');
  const scenarios = readdirSync(directory).filter(name => name.startsWith('helper-'));

  for (const name of scenarios) {
    test(name, async () => {
      const scenario = JSON.parse(readFileSync(join(directory, name), 'utf8')) as {
        steps: {dir: 'c2s' | 's2c'; frame?: unknown; expect?: unknown}[];
      };
      const process = new FakeHelper().spawn();
      const lines: unknown[] = [];
      let buffer = '';
      let wake: () => void = () => {};
      process.stdout.setEncoding('utf8');
      process.stdout.on('data', (chunk: string) => {
        buffer += chunk;
        for (let newline = buffer.indexOf('\n'); newline >= 0; newline = buffer.indexOf('\n')) {
          lines.push(JSON.parse(buffer.slice(0, newline)));
          buffer = buffer.slice(newline + 1);
        }
        wake();
      });
      for (const step of scenario.steps) {
        if (step.dir === 'c2s') {
          process.stdin.write(`${JSON.stringify(step.frame)}\n`);
          continue;
        }
        while (lines.length === 0) await new Promise<void>(resolve => (wake = resolve));
        expect(lines.shift()).toMatchObject(step.expect as object);
      }
      process.kill('SIGTERM');
    });
  }
});
