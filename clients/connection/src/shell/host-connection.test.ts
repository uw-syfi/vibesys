import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readdirSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import type {ChannelEndOutcome} from '@vibesys/contracts/host-helper';
import {FakeClock} from '@vibesys/transport/testing';
import type {Cause, ConnectionConfig} from '../state/host-connection.js';
import {ECHO_SERVER, FakeHelper, FakeLink, record} from '../testing/fake-helper.js';
import {fakeProcess} from '../testing/fake-process.js';
import {hostConnectionContract, phaseReached} from '../testing/host-connection-contract.js';
import {ConnectionManager} from './connection-manager.js';
import {ConnectionError, type HelperRequestError} from './helper-channel.js';
import {HostConnection} from './host-connection.js';
import {LocalLink, nodeSpawn, type Spawn, SshLink} from './link.js';

const CONFIG: ConnectionConfig = {delaysMs: [500, 1_000], deadlineMs: 60_000, heartbeatMs: 15_000};
const REPOSITORY = join(import.meta.dir, '../../../..');
const realContracts = process.env['VIBESYS_REAL_CONTRACTS'] === '1';
const GPU = {kind: 'ssh', alias: 'gpu'} as const;

function fakeConnection(
  link = new FakeLink(),
  clock = new FakeClock(),
  checkout = '/src/vibesys',
): HostConnection {
  return new HostConnection({
    link,
    checkout,
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
  });
}

function fakeManager(linkFor: () => FakeLink | SshLink, clock = new FakeClock()) {
  return new ConnectionManager({
    linkFor,
    appVersion: 'test',
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
    config: CONFIG,
  });
}

async function rejection(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('resolved instead of rejecting');
}

hostConnectionContract('fake', {
  working: () => fakeConnection(),
  broken: () => {
    const link = new FakeLink();
    link.helper.bootstrapExit = {code: 65, stdout: 'nodir'};
    return fakeConnection(link);
  },
});

/** The real helper of `checkout`, on This Mac, with a registry of its own. */
function realConnection(checkout: string): HostConnection {
  const runtime = mkdtempSync(join(tmpdir(), 'vsd-contract-'));
  const link = new LocalLink((command, args, env) =>
    nodeSpawn(command, args, {...env, XDG_RUNTIME_DIR: runtime}),
  );
  return new HostConnection({
    link,
    checkout,
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

/**
 * `ssh` over a `FakeHelper` host: `-O check` answers whether a master is up, the master command
 * brings one up, and every other command is a helper channel. It records what was started.
 */
function fakeSsh(helper: FakeHelper) {
  const seen = {masters: 0, helpers: [] as string[]};
  let up = false;
  const spawn: Spawn = (_command, args) => {
    const control = args.indexOf('-O');
    const last = args.at(-1) ?? '';
    if (control >= 0) {
      const process = fakeProcess();
      if (args[control + 1] === 'exit') up = false;
      process.end(args[control + 1] === 'check' && !up ? 255 : 0);
      return process.process;
    }
    if (last === 'true') {
      seen.masters += 1;
      up = true;
      const process = fakeProcess();
      process.end(0);
      return process.process;
    }
    seen.helpers.push(last);
    return helper.spawn();
  };
  return {spawn, seen};
}

function sshLink(spawn: Spawn): SshLink {
  return new SshLink({alias: 'gpu', controlPath: '/tmp/cm-%C', askpass: '/askpass', spawn});
}

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

  test('registry-watch setup failures cannot publish a watchless control channel', async () => {
    const clock = new FakeClock();
    const link = new FakeLink();
    link.helper.expired.add('instances.watch');
    await using connection = fakeConnection(link, clock);

    const ready = connection.ready();
    for (const [index, delay] of CONFIG.delaysMs.entries()) {
      await clock.waitUntilScheduled(delay);
      expect(link.helper.running).toBe(0);
      expect(link.helper.watching).toBe(0);
      if (index === CONFIG.delaysMs.length - 1) {
        link.helper.expired.delete('instances.watch');
      }
      clock.advanceBy(delay);
    }
    await ready;

    expect(connection.phase.kind).toBe('ready');
    expect(link.helper.running).toBe(1);
    expect(link.helper.watching).toBe(1);
    const pushed = new Promise<string[]>(resolve => {
      connection.watchInstances(listing => {
        if (listing.instances.length > 0) resolve(listing.instances.map(entry => entry.id));
      });
    });
    link.helper.publish(record('00000000000f', 'run-f'));
    expect(await pushed).toEqual(['00000000000f']);
  });

  test('a control channel ending after its watch reply cannot complete the handshake', async () => {
    const clock = new FakeClock();
    const link = new FakeLink();
    const watched = link.helper.deferNext('instances.watch');
    await using connection = fakeConnection(link, clock);
    const nextPhase = new Promise<string>(resolve => {
      const stop = connection.onPhase(phase => {
        if (phase.kind !== 'ready' && phase.kind !== 'reconnecting') return;
        stop();
        resolve(phase.kind);
      });
    });

    const ready = connection.ready();
    await watched.received;
    watched.release();
    link.helper.channels.at(-1)?.end(null);

    expect(await nextPhase).toBe('reconnecting');
    expect(connection.checkoutRoot).toBeNull();
    expect(link.helper.watching).toBe(0);
    await clock.waitUntilScheduled(CONFIG.delaysMs[0] ?? 0);
    clock.advanceBy(CONFIG.delaysMs[0] ?? 0);
    await ready;
    expect(connection.phase.kind).toBe('ready');
    expect(link.helper.watching).toBe(1);
  });

  test('a refused sign-in stops the loop until the user retries', async () => {
    const link = new FakeLink();
    link.linkFault = {cause: 'auth', detail: 'Permission denied'};
    await using connection = fakeConnection(link);
    const error = await rejection(connection.ready());
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

  test('a link that is down walks the whole schedule, goes offline, and a wake starts over', async () => {
    const clock = new FakeClock();
    const link = new FakeLink();
    await using connection = fakeConnection(link, clock);
    await connection.ready();
    link.linkFault = {cause: 'link', detail: 'down'};
    link.helper.drop();
    // Each failed check arms the next slot of the schedule, the first one included.
    for (const delay of CONFIG.delaysMs) {
      await clock.waitUntilScheduled(delay);
      clock.advanceBy(delay);
    }
    await phaseReached(connection, 'offline');
    link.linkFault = null;
    connection.dispatch({type: 'wake'});
    await phaseReached(connection, 'ready');
  });

  test('regression: closing resolves only once every helper process has exited', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000c', 'run-c'));
    link.helper.host.runs.set('00000000000c', ECHO_SERVER);
    const connection = fakeConnection(link);
    const {stream} = await connection.openRun('00000000000c');
    const ended = new Promise<void>(resolve => stream.once('close', () => resolve()));
    expect(link.helper.running).toBe(2);
    await connection[Symbol.asyncDispose]();
    expect(link.helper.running).toBe(0);
    await connection[Symbol.asyncDispose]();
    await ended;
    expect(link.released).toBe(0);
  });

  test('closing while the helper has not answered ends that helper too', async () => {
    const link = new FakeLink();
    link.helper.silent = true;
    const connection = fakeConnection(link);
    const ready = rejection(connection.ready());
    expect(connection.phase.kind).toBe('linking');
    await phaseReached(connection, 'handshaking');
    await connection[Symbol.asyncDispose]();
    expect(link.helper.running).toBe(0);
    expect(await ready).toBeInstanceOf(ConnectionError);
    // A closed connection starts nothing.
    expect(await rejection(connection.openRun('00000000000c'))).toBeInstanceOf(ConnectionError);
    expect(link.helper.spawned).toBe(1);
  });

  test('regression: a handshake past its deadline is aborted and never installs its channel', async () => {
    const clock = new FakeClock();
    const link = new FakeLink();
    link.helper.silent = true;
    await using connection = fakeConnection(link, clock);
    void connection.ready().catch(() => {});
    await phaseReached(connection, 'handshaking');
    const [held] = link.helper.channels;
    clock.advanceBy(CONFIG.deadlineMs);
    expect(connection.phase.kind).toBe('reconnecting');
    // The abandoned helper is ended at once, not when its own request deadline passes.
    await held?.process.exit;
    link.helper.answerHeld();
    expect(connection.checkoutRoot).toBeNull();
    expect(link.helper.watching).toBe(0);
  });

  test('a run this user may not connect to fails denied, and the host stays ready', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000d', 'run-d'));
    link.helper.host.runs.set('00000000000d', ECHO_SERVER);
    link.helper.host.denied.add('00000000000d');
    await using connection = fakeConnection(link);
    const error = await rejection(connection.openRun('00000000000d'));
    expect((error as ConnectionError).reason).toBe('denied');
    expect(connection.phase.kind).toBe('ready');
  });

  test('a request the helper answers past its deadline fails with that code; a late ping is a lost link', async () => {
    const clock = new FakeClock();
    const link = new FakeLink();
    link.helper.host.projects.set('/p', ['spsc']);
    link.helper.expired.add('tasks.list');
    await using connection = fakeConnection(link, clock);
    const error = await rejection(connection.request({type: 'tasks.list', project: '/p'}));
    expect((error as HelperRequestError).code).toBe('deadline_exceeded');
    expect(connection.phase.kind).toBe('ready');
    link.helper.expired.add('ping');
    clock.advanceBy(CONFIG.heartbeatMs);
    await phaseReached(connection, 'reconnecting');
  });

  test('a reply outside the contract ends the channel as incompatible', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/p', ['spsc']);
    link.helper.garbled.add('tasks.list');
    await using connection = fakeConnection(link);
    const error = await rejection(connection.request({type: 'tasks.list', project: '/p'}));
    expect((error as ConnectionError).reason).toBe('incompatible');
    await phaseReached(connection, 'incompatible');
  });

  const bootstraps: readonly {code: number; stdout: string; says: string}[] = [
    {code: 64, stdout: 'predates 0.0.9', says: 'predates the desktop helper (version 0.0.9)'},
    {code: 65, stdout: 'nodir', says: '/src/vibesys on fake is not a directory.'},
    {code: 65, stdout: 'nopyproject', says: 'has no pyproject.toml'},
    {code: 65, stdout: 'notvibesys', says: 'does not declare the vibesys project'},
    {code: 66, stdout: 'missing', says: 'uv was not found on fake'},
    // A reserved status without its own line is not the bootstrap's report.
    {code: 64, stdout: 'Welcome to the cluster', says: 'exited with 64'},
    {code: 65, stdout: 'Traceback (most recent call last):', says: 'exited with 65'},
    {code: 66, stdout: '', says: 'exited with 66'},
  ];
  for (const {code, stdout, says} of bootstraps) {
    test(`a bootstrap that exits ${code} saying ${JSON.stringify(stdout)} stops as incompatible`, async () => {
      const link = new FakeLink();
      link.helper.bootstrapExit = {code, stdout};
      await using connection = fakeConnection(link);
      const error = await rejection(connection.ready());
      expect((error as ConnectionError).reason).toBe('incompatible');
      expect(error.message).toContain(says);
    });
  }

  test('regression: a helper that exits 65 after its handshake is not a bootstrap report', async () => {
    const link = new FakeLink();
    link.helper.exitAfterHello = {code: 65, stdout: 'notvibesys'};
    await using connection = fakeConnection(link);
    await rejection(connection.ready());
    await phaseReached(connection, 'incompatible');
    const phase = connection.phase;
    expect(phase.kind === 'incompatible' ? phase.detail : '').toContain('exited with 65');
  });

  const outcomes: Readonly<Record<ChannelEndOutcome, Cause | null>> = {
    client_closed: null,
    server_closed: null,
    run_gone: 'run-gone',
    connect_denied: 'denied',
    connect_failed: 'link',
    server_stalled: 'link',
    client_stalled: 'link',
    server_failed: 'link',
    client_failed: 'link',
  };
  for (const [outcome, cause] of Object.entries(outcomes) as [ChannelEndOutcome, Cause | null][]) {
    test(`a relay that ends ${outcome} ends its stream ${cause ?? 'normally'}`, async () => {
      const link = new FakeLink();
      link.helper.publish(record('00000000000e', 'run-e'));
      link.helper.host.runs.set('00000000000e', stream => stream.onLine(() => stream.end(outcome)));
      await using connection = fakeConnection(link);
      const {stream} = await connection.openRun('00000000000e');
      const ended = new Promise<Error | null>(resolve => {
        stream.once('error', error => resolve(error));
        stream.once('end', () => resolve(null));
        stream.resume();
      });
      stream.write('go\n');
      const error = await ended;
      expect(error === null ? null : (error as ConnectionError).reason).toBe(cause);
      if (cause === 'link') {
        // A link-class end is checked with a fresh run.connect, which finds the run there.
        await phaseReached(connection, 'reconnecting');
        await phaseReached(connection, 'ready');
      } else {
        expect(connection.phase.kind).toBe('ready');
      }
    });
  }
});

describe('ConnectionManager', () => {
  test('regression: releasing a link kills an in-flight ssh master attempt instead of waiting it out', async () => {
    const actions: string[] = [];
    const killed: string[] = [];
    const check = fakeProcess();
    const master = fakeProcess(signal => killed.push(signal));
    let announceMaster = (): void => {};
    const masterStarted = new Promise<void>(resolve => {
      announceMaster = resolve;
    });
    const spawn: Spawn = (_command, args) => {
      const control = args.indexOf('-O');
      const operation = control < 0 ? null : args[control + 1];
      if (operation === 'check') {
        actions.push('check');
        return check.process;
      }
      if (operation === 'exit') {
        actions.push('exit');
        const exiting = fakeProcess();
        exiting.end(0);
        return exiting.process;
      }
      if (args.at(-1) === 'true') {
        actions.push('master-start');
        announceMaster();
        return master.process;
      }
      throw new Error(`unexpected ssh operation: ${args.join(' ')}`);
    };
    const link = sshLink(spawn);
    const establishing = link.establish(false);
    expect(actions).toEqual(['check']);
    check.end(255);
    await masterStarted;

    // The master-creation command can legitimately run for its 20s ConnectTimeout; nothing here
    // ever finishes it on its own, modeling a handshake still under way when the app quits.
    // Release must not wait that out: it kills the attempt itself, rather than leaving an ssh
    // process free to fork off a persistent master nobody ever asks to exit.
    const closing = link.release();
    expect(killed).toEqual(['SIGTERM']);
    expect(actions).toEqual(['check', 'master-start']);

    const error = await rejection(establishing);
    expect((error as ConnectionError).reason).toBe('link');
    await closing;

    expect(actions).toEqual(['check', 'master-start', 'exit']);
  });

  test('releasing after the master check resolves never starts a late master attempt', async () => {
    const actions: string[] = [];
    const check = fakeProcess();
    const spawn: Spawn = (_command, args) => {
      const control = args.indexOf('-O');
      const operation = control < 0 ? null : args[control + 1];
      if (operation === 'check') {
        actions.push('check');
        return check.process;
      }
      if (operation === 'exit') {
        actions.push('exit');
        const exiting = fakeProcess();
        exiting.end(0);
        return exiting.process;
      }
      actions.push('master-start');
      const master = fakeProcess();
      master.end(0);
      return master.process;
    };
    const link = sshLink(spawn);
    const establishing = link.establish(false);
    check.end(255);

    // End the link before the completed check's continuation can start a master process.
    const closing = link.release();

    expect(await rejection(establishing)).toBeInstanceOf(ConnectionError);
    await closing;
    expect(await rejection(link.establish(false))).toBeInstanceOf(ConnectionError);
    expect(actions).toEqual(['check', 'exit']);
  });

  test('a checkout check shares the host link, and the manager releases it once, at the end', async () => {
    const link = new FakeLink();
    let links = 0;
    const manager = fakeManager(() => {
      links += 1;
      return link;
    });
    const kept = manager.connection(GPU, '/src/vibesys');
    await kept.ready();
    {
      // regression: closing a checkout check ended the ssh master the kept connection used.
      await using candidate = manager.candidate(GPU, '/src/other');
      await candidate.ready();
    }
    expect(link.released).toBe(0);
    await kept.request({type: 'ping'});
    let runningAtRelease = -1;
    link.release = async () => {
      runningAtRelease = link.helper.running;
    };
    await manager[Symbol.asyncDispose]();
    expect(runningAtRelease).toBe(0);
    expect(kept.phase.kind).toBe('closed');
    expect(links).toBe(1);
    expect(link.helper.checkouts).toContain('/src/other');
  });

  test('regression: a changed checkout closes the replaced connection once no run holds it', async () => {
    const link = new FakeLink();
    const manager = fakeManager(() => link);
    const first = manager.connection(GPU, '/a');
    await first.ready();
    const release = first.hold();
    const second = manager.connection(GPU, '/b');
    expect(manager.existing('ssh:gpu')).toBe(second);
    await second.ready();
    // The attached run still dials through the connection it was attached with.
    expect(first.phase.kind).toBe('ready');
    release();
    expect(first.phase.kind).toBe('closed');
    // Nothing holds this one: replacing it closes it at once.
    manager.connection(GPU, '/c');
    expect(second.phase.kind).toBe('closed');
    await manager[Symbol.asyncDispose]();
    expect(link.helper.running).toBe(0);
  });

  test('regression: connections that open together share one ssh master', async () => {
    const helper = new FakeHelper();
    const {spawn, seen} = fakeSsh(helper);
    const manager = fakeManager(() => sshLink(spawn));
    const kept = manager.connection(GPU, '/a');
    await using candidate = manager.candidate(GPU, '/b');
    await Promise.all([kept.ready(), candidate.ready()]);
    expect(seen.masters).toBe(1);
    await manager[Symbol.asyncDispose]();
  });

  test('a link passes the uv its first bootstrap found to the next one', async () => {
    const helper = new FakeHelper();
    helper.preamble = ['uv /opt/uv/bin/uv'];
    const {spawn, seen} = fakeSsh(helper);
    const manager = fakeManager(() => sshLink(spawn));
    const connection = manager.connection(GPU, '/a');
    await connection.ready();
    helper.preamble = [];
    helper.publish(record('00000000000f', 'run-f'));
    helper.host.runs.set('00000000000f', ECHO_SERVER);
    const {stream} = await connection.openRun('00000000000f');
    stream.destroy();
    const uv = `x${Buffer.from('/opt/uv/bin/uv').toString('hex')}`;
    expect(seen.helpers.map(command => command.split(' ').at(-1))).toEqual(['x', uv]);
    await manager[Symbol.asyncDispose]();
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
