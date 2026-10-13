import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readdirSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import type {ChannelEndOutcome} from '@vibesys/contracts/host-helper';
import {FakeClock} from '@vibesys/transport/testing';
import type {Cause, ConnectionConfig} from '../state/host-connection.js';
import {ECHO_SERVER, FakeHelper, FakeLink, record} from '../testing/fake-helper.js';
import {deferredKillProcess, fakeProcess} from '../testing/fake-process.js';
import {hostConnectionContract, phaseReached} from '../testing/host-connection-contract.js';
import {ConnectionManager} from './connection-manager.js';
import {ConnectionError, HelperRequestError} from './helper-channel.js';
import {HostConnection} from './host-connection.js';
import {LocalLink, nodeSpawn, type Spawn, SshLink} from './link.js';

const CONFIG: ConnectionConfig = {delaysMs: [500, 1_000], deadlineMs: 60_000, heartbeatMs: 15_000};
const REPOSITORY = join(import.meta.dir, '../../../../..');
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

class HeldReleaseLink extends FakeLink {
  readonly releaseStarted: Promise<void>;
  readonly #started: () => void;
  readonly #finished: Promise<void>;
  readonly #finish: () => void;

  constructor() {
    super();
    let started = (): void => {};
    let finish = (): void => {};
    this.releaseStarted = new Promise(resolve => {
      started = resolve;
    });
    this.#finished = new Promise(resolve => {
      finish = resolve;
    });
    this.#started = started;
    this.#finish = finish;
  }

  override async release(): Promise<void> {
    this.released += 1;
    this.#started();
    await this.#finished;
  }

  finishRelease(): void {
    this.#finish();
  }
}

class FailedReleaseLink extends FakeLink {
  readonly failure = new ConnectionError('link', 'the old master is still reachable');

  override async release(): Promise<void> {
    this.released += 1;
    throw this.failure;
  }
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

function sshLink(spawn: Spawn, clock = new FakeClock()): SshLink {
  return new SshLink({
    alias: 'gpu',
    controlPath: '/tmp/cm-%C',
    askpass: '/askpass',
    spawn,
    scheduleTimeout: clock.scheduleTimeout,
    retirementCommandTimeoutMs: 100,
  });
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

  test('does not send inspection to a helper that did not negotiate it', async () => {
    const link = new FakeLink();
    link.helper.features.delete('instances_inspect');
    await using connection = fakeConnection(link);

    const error: unknown = await connection
      .request({type: 'instances.inspect', instance_id: '0123456789ab', include_log: false})
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(HelperRequestError);
    if (!(error instanceof HelperRequestError)) throw error;
    expect(error.code).toBe('feature_not_negotiated');
    expect(connection.phase.kind).toBe('ready');
  });

  test('never writes any feature-gated request when hello omitted its feature', async () => {
    const link = new FakeLink();
    link.helper.features.clear();
    await using connection = fakeConnection(link);
    await connection.ready();

    const errors = await Promise.all([
      rejection(connection.request({type: 'instances.watch'})),
      rejection(connection.request({type: 'instances.unwatch', watch_id: 'missing'})),
      rejection(
        connection.request({
          type: 'instances.inspect',
          instance_id: '0123456789ab',
          include_log: false,
        }),
      ),
      rejection(connection.request({type: 'paths.complete', prefix: '/'})),
    ]);

    expect(errors).toHaveLength(4);
    for (const error of errors) {
      expect(error).toBeInstanceOf(HelperRequestError);
      expect((error as HelperRequestError).code).toBe('feature_not_negotiated');
    }
    const gated = new Set([
      'instances.watch',
      'instances.unwatch',
      'instances.inspect',
      'paths.complete',
    ]);
    expect(link.helper.requests.filter(request => gated.has(request.type))).toEqual([]);
    await expect(connection.request({type: 'ping'})).resolves.toMatchObject({type: 'ping'});
    expect(connection.phase.kind).toBe('ready');
  });

  test('without instances_watch, each control channel installs one snapshot and never polls', async () => {
    const link = new FakeLink();
    link.helper.features.delete('instances_watch');
    link.helper.publish(record('000000000001', 'run-1'));
    await using connection = fakeConnection(link);
    const listings: string[][] = [];
    connection.watchInstances(listing =>
      listings.push(listing.instances.map(instance => instance.id)),
    );

    await connection.ready();
    expect(listings).toEqual([['000000000001']]);
    expect(link.helper.requests.filter(request => request.type === 'instances.list')).toHaveLength(
      1,
    );
    expect(link.helper.watching).toBe(0);

    link.helper.publish(record('000000000002', 'run-2'));
    expect(listings).toEqual([['000000000001']]);
    expect(link.helper.requests.filter(request => request.type === 'instances.list')).toHaveLength(
      1,
    );

    const reconnecting = phaseReached(connection, 'reconnecting');
    link.helper.drop();
    await reconnecting;
    await phaseReached(connection, 'ready');

    expect(listings).toEqual([['000000000001'], ['000000000001', '000000000002']]);
    expect(link.helper.requests.filter(request => request.type === 'instances.list')).toHaveLength(
      2,
    );
    expect(link.helper.watching).toBe(0);
  });

  test('inspection requests retained diagnostics only with explicit log consent', async () => {
    const link = new FakeLink();
    const instanceId = '0123456789ab';
    link.helper.host.logs.set(instanceId, {text: 'retained diagnostics', truncated: false});
    await using connection = fakeConnection(link);

    const metadataOnly = await connection.request({
      type: 'instances.inspect',
      instance_id: instanceId,
      include_log: false,
    });
    const withLog = await connection.request({
      type: 'instances.inspect',
      instance_id: instanceId,
      include_log: true,
    });

    expect(metadataOnly.inspection.log).toBeNull();
    expect(withLog.inspection.log).toEqual({
      state: 'available',
      text: 'retained diagnostics',
      truncated: false,
    });
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
    const {runId, stream} = await connection.openRun('00000000000b', 'run-control');
    expect(runId).toBe('run-b');
    const echoed = new Promise<string>(resolve =>
      stream.once('data', (chunk: Buffer) => resolve(chunk.toString())),
    );
    stream.write('{"type":"query.snapshot"}\n');
    expect(JSON.parse(await echoed)).toEqual({echo: '{"type":"query.snapshot"}'});
    stream.destroy();
    expect(link.roles).toEqual(['host-control', 'run-control']);
    expect(link.helper.connectRoles).toEqual(['control']);
  });

  test('maps every semantic run role to the host-helper wire role', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000a', 'run-a'));
    link.helper.host.runs.set('00000000000a', ECHO_SERVER);
    await using connection = fakeConnection(link);
    for (const role of ['run-control', 'subscribe', 'chat'] as const) {
      const {stream} = await connection.openRun('00000000000a', role);
      stream.destroy();
    }
    expect(link.roles).toEqual(['host-control', 'run-control', 'subscribe', 'chat']);
    expect(link.helper.connectRoles).toEqual(['control', 'subscribe', 'chat']);
  });

  test('a coalesced connect reply preserves the first run frame at every byte split', async () => {
    const greeting = '{"type":"event_batch","events":[]}';
    const reply = JSON.stringify({
      type: 'run.connect',
      request_id: 'r2',
      run_id: 'run-b',
      protocol_version: 1,
    });
    const payloadLength = Buffer.byteLength(`${reply}\n${greeting}\n`);

    for (let split = 0; split <= payloadLength; split += 1) {
      const link = new FakeLink();
      link.helper.publish(record('00000000000b', 'run-b'));
      link.helper.host.runs.set('00000000000b', ECHO_SERVER);
      link.helper.runGreeting = greeting;
      link.helper.runConnectChunks = payload => [
        payload.subarray(0, split),
        payload.subarray(split),
      ];
      await using connection = fakeConnection(link);

      const {runId, stream} = await connection.openRun('00000000000b', 'run-control');
      const expected = Buffer.from(`${greeting}\n`);
      const delivered = await new Promise<Buffer>(resolve => {
        const chunks: Buffer[] = [];
        let length = 0;
        stream.on('data', (chunk: Buffer) => {
          chunks.push(chunk);
          length += chunk.length;
          if (length >= expected.length) resolve(Buffer.concat(chunks));
        });
      });

      expect(runId).toBe('run-b');
      expect(delivered).toEqual(expected);
      expect(connection.phase.kind).toBe('ready');
      expect(link.helper.running).toBe(2);
      stream.destroy();
    }
  });

  test('an invalid run.connect reply never transfers the channel to relay mode', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000b', 'run-b'));
    link.helper.host.runs.set('00000000000b', ECHO_SERVER);
    link.helper.garbled.add('run.connect');
    await using connection = fakeConnection(link);
    await connection.ready();

    const error = await rejection(connection.openRun('00000000000b', 'run-control'));

    expect(error).toBeInstanceOf(ConnectionError);
    if (!(error instanceof ConnectionError)) throw error;
    expect(error.reason).toBe('incompatible');
    expect(connection.phase.kind).toBe('incompatible');
    const runChannel = link.helper.channels.at(-1);
    if (runChannel === undefined) throw new Error('the run helper was not spawned');
    expect(await runChannel.process.exit).toBeNull();
    expect(link.helper.running).toBe(1);
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

  test('regression: wake abandons a stalled link and starts a fresh operation', async () => {
    const link = new FakeLink();
    const stale = link.deferEstablish();
    await using connection = fakeConnection(link);
    const ready = connection.ready();
    await stale.received;
    const fresh = link.deferEstablish();

    connection.dispatch({type: 'wake'});

    expect(stale.abandoned).toBe(true);
    expect(link.establishments).toBe(2);
    await fresh.received;
    fresh.release();
    await ready;
    expect(connection.phase.kind).toBe('ready');
  });

  test('cancelled run dials leave no readiness work to replay after the host recovers', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000c', 'run-c'));
    link.helper.host.runs.set('00000000000c', ECHO_SERVER);
    const establishing = link.deferEstablish();
    await using connection = fakeConnection(link);
    const controllers = Array.from({length: 4}, () => new AbortController());
    const dials = controllers.map(controller =>
      rejection(connection.openRun('00000000000c', 'run-control', controller.signal)),
    );
    await establishing.received;

    const reasons = controllers.map((controller, index) => {
      const reason = new DOMException(`page ${index} closed`, 'AbortError');
      controller.abort(reason);
      return reason;
    });
    expect(await Promise.all(dials)).toEqual(reasons);
    expect(link.helper.spawned).toBe(0);

    establishing.release();
    await phaseReached(connection, 'ready');
    expect(connection.phase.kind).toBe('ready');
    // Only the recovered control helper exists: no abandoned run dial started a helper later.
    expect(link.helper.spawned).toBe(1);
    expect(link.helper.running).toBe(1);
  });

  test('cancelling an established run channel is cleanup, not a host failure', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000c', 'run-c'));
    link.helper.host.runs.set('00000000000c', ECHO_SERVER);
    await using connection = fakeConnection(link);
    const controller = new AbortController();
    const {stream} = await connection.openRun('00000000000c', 'run-control', controller.signal);
    const closed = new Promise<void>(resolve => stream.once('close', resolve));
    stream.on('error', () => {});

    controller.abort();
    await closed;

    expect(connection.phase.kind).toBe('ready');
    expect(link.helper.running).toBe(1);
  });

  test('regression: closing resolves only once every helper process has exited', async () => {
    const link = new FakeLink();
    link.helper.publish(record('00000000000c', 'run-c'));
    link.helper.host.runs.set('00000000000c', ECHO_SERVER);
    const connection = fakeConnection(link);
    const {stream} = await connection.openRun('00000000000c', 'run-control');
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
    expect(await rejection(connection.openRun('00000000000c', 'run-control'))).toBeInstanceOf(
      ConnectionError,
    );
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
    const error = await rejection(connection.openRun('00000000000d', 'run-control'));
    expect((error as ConnectionError).reason).toBe('denied');
    expect(connection.phase.kind).toBe('ready');
  });

  // regression: openRun's own catch used to dispatch `run-ended` only when the dial failed with
  // cause 'link', so 'incompatible' (and 'auth', the same code path) never reached the machine: the
  // host stayed ready forever and every later attach failed the same generic way. It now forwards
  // whatever cause the dial actually failed with, same as `#connectRun`'s stream-error listener; the
  // machine's own routing for each cause (`run-ended` -> `lost` -> `failed`) is covered by its model
  // tests in `../state/host-connection.test.ts`, so this only proves the shell forwards what it saw.
  const dialFailures: readonly {
    readonly cause: Cause;
    readonly nextPhase: string;
    readonly arrange: (link: FakeLink) => void;
  }[] = [
    {
      cause: 'link',
      nextPhase: 'reconnecting',
      arrange: link => {
        link.linkFault = {cause: 'link', detail: 'down'};
      },
    },
    {
      cause: 'incompatible',
      nextPhase: 'incompatible',
      arrange: link => {
        // The checkout turns incompatible on disk after the control channel is already up: the
        // next helper this link starts (for the run channel) fails its bootstrap before any
        // `hello` reply.
        link.helper.bootstrapExit = {code: 65, stdout: 'notvibesys'};
      },
    },
    {
      cause: 'auth',
      nextPhase: 'auth-needed',
      arrange: link => {
        link.linkFault = {cause: 'auth', detail: 'Permission denied'};
      },
    },
    {cause: 'run-gone', nextPhase: 'ready', arrange: () => {}},
    {
      cause: 'denied',
      nextPhase: 'ready',
      arrange: link => {
        link.helper.publish(record('0000000000f0', 'run-f0'));
        link.helper.host.runs.set('0000000000f0', ECHO_SERVER);
        link.helper.host.denied.add('0000000000f0');
      },
    },
  ];
  for (const {cause, nextPhase, arrange} of dialFailures) {
    test(`openRun's own dial failure reaches the machine as ${cause}`, async () => {
      const link = new FakeLink();
      await using connection = fakeConnection(link);
      await connection.ready();
      arrange(link);
      const error = await rejection(connection.openRun('0000000000f0', 'run-control'));
      expect((error as ConnectionError).reason).toBe(cause);
      await phaseReached(connection, nextPhase);
    });
  }

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
      const {stream} = await connection.openRun('00000000000e', 'run-control');
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
  test('uncontended acquisitions register host ownership before returning', async () => {
    const links: FakeLink[] = [];
    const manager = fakeManager(() => {
      const link = new FakeLink();
      links.push(link);
      return link;
    });

    const acquiring = manager.acquire(GPU, '/shown');
    expect(links).toHaveLength(1);
    const checking = manager.candidate({kind: 'ssh', alias: 'candidate'}, '/candidate');
    expect(links).toHaveLength(2);
    const signingIn = manager.signIn({kind: 'ssh', alias: 'sign-in'});
    expect(links).toHaveLength(3);

    const lease = await acquiring;
    const candidate = await checking;
    await signingIn;
    await candidate[Symbol.asyncDispose]();
    await lease[Symbol.asyncDispose]();
    await manager[Symbol.asyncDispose]();
  });

  test('releasing the final connection lease retires its helper and host link', async () => {
    const link = new FakeLink();
    const manager = fakeManager(() => link);
    const lease = await manager.acquire(GPU, '/src/vibesys');
    const connection = lease.connection;
    await connection.ready();

    await lease[Symbol.asyncDispose]();

    expect(manager.existing('ssh:gpu')).toBeNull();
    expect(connection.phase.kind).toBe('closed');
    expect(link.helper.running).toBe(0);
    expect(link.released).toBe(1);
    await manager[Symbol.asyncDispose]();
    expect(link.released).toBe(1);
  });

  test('a candidate and sign-in keep the link alive after its final shown lease retires', async () => {
    const link = new FakeLink();
    const establishing = link.deferEstablish();
    const manager = fakeManager(() => link);
    const lease = await manager.acquire(GPU, '/shown');
    const candidate = await manager.candidate(GPU, '/candidate');
    const signingIn = manager.signIn(GPU);
    await establishing.received;

    await lease[Symbol.asyncDispose]();
    expect(lease.connection.phase.kind).toBe('closed');
    expect(link.released).toBe(0);

    await candidate[Symbol.asyncDispose]();
    expect(link.released).toBe(0);
    establishing.release();
    await signingIn;
    expect(link.released).toBe(1);
    await manager[Symbol.asyncDispose]();
  });

  test('a retired connection held by an attached run closes only after that hold ends', async () => {
    const link = new FakeLink();
    const manager = fakeManager(() => link);
    const lease = await manager.acquire(GPU, '/src/vibesys');
    await lease.connection.ready();
    const releaseRun = lease.connection.hold();

    await lease[Symbol.asyncDispose]();
    expect(manager.existing('ssh:gpu')).toBeNull();
    expect(lease.connection.phase.kind).toBe('ready');
    expect(link.released).toBe(0);

    releaseRun();
    expect(lease.connection.phase.kind).toBe('closed');
    await lease.connection[Symbol.asyncDispose]();
    expect(link.released).toBe(1);
    await manager[Symbol.asyncDispose]();
  });

  test('retiring a view lease cannot close an in-flight request owned by another lease', async () => {
    const link = new FakeLink();
    const manager = fakeManager(() => link);
    const view = await manager.acquire(GPU, '/src/vibesys');
    const request = await manager.acquire(GPU, '/src/vibesys');
    await request.connection.ready();
    const answer = link.helper.deferNext('instances.list');
    const listing = request.connection.request({type: 'instances.list'});
    await answer.received;

    await view[Symbol.asyncDispose]();
    expect(request.connection.phase.kind).toBe('ready');
    expect(link.released).toBe(0);

    answer.release();
    await listing;
    await request[Symbol.asyncDispose]();
    expect(request.connection.phase.kind).toBe('closed');
    expect(link.released).toBe(1);
    await manager[Symbol.asyncDispose]();
  });

  test('all host acquisitions wait for the prior link release to settle', async () => {
    const first = new HeldReleaseLink();
    const second = new FakeLink();
    const links = [first, second];
    const manager = fakeManager(() => {
      const link = links.shift();
      if (link === undefined) throw new Error('unexpected third link');
      return link;
    });
    const obsolete = await manager.acquire(GPU, '/a');
    await obsolete.connection.ready();

    const retiring = obsolete[Symbol.asyncDispose]();
    await first.releaseStarted;
    const acquiring = manager.acquire(GPU, '/a');
    const checking = manager.candidate(GPU, '/candidate');
    const signingIn = manager.signIn(GPU);

    // No operation may consume the replacement while the old control path can still be exited.
    expect(links).toEqual([second]);
    expect(second.helper.running).toBe(0);

    first.finishRelease();
    await retiring;
    const current = await acquiring;
    const candidate = await checking;
    await signingIn;
    await current.connection.ready();

    expect(current.connection).not.toBe(obsolete.connection);
    expect(second.helper.running).toBe(1);
    await candidate[Symbol.asyncDispose]();
    await current[Symbol.asyncDispose]();
    expect(second.released).toBe(1);
    await manager[Symbol.asyncDispose]();
  });

  test('a failed link release remains an admission barrier and is surfaced', async () => {
    const first = new FailedReleaseLink();
    const replacement = new FakeLink();
    const links = [first, replacement];
    const logged: string[] = [];
    const manager = new ConnectionManager({
      linkFor: () => {
        const link = links.shift();
        if (link === undefined) throw new Error('unexpected third link');
        return link;
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: message => logged.push(message),
      config: CONFIG,
    });
    const obsolete = await manager.acquire(GPU, '/a');
    await obsolete.connection.ready();

    await obsolete[Symbol.asyncDispose]();
    const acquisitionError = await rejection(manager.acquire(GPU, '/b'));

    expect(acquisitionError).toBe(first.failure);
    expect(first.released).toBe(1);
    expect(links).toEqual([replacement]);
    expect(replacement.helper.running).toBe(0);
    expect(logged.at(-1)).toBe('could not retire ssh:gpu: the old master is still reachable');
    expect(await rejection(manager[Symbol.asyncDispose]())).toBe(first.failure);
  });

  test('manager disposal surfaces an active link retirement failure', async () => {
    const link = new FailedReleaseLink();
    const manager = fakeManager(() => link);
    const lease = await manager.acquire(GPU, '/active');
    await lease.connection.ready();

    const error = await rejection(manager[Symbol.asyncDispose]());

    expect(error).toBe(link.failure);
    expect(link.released).toBe(1);
    expect(lease.connection.phase.kind).toBe('closed');
  });

  test('manager disposal settles every host before surfacing a retirement failure', async () => {
    const failed = new FailedReleaseLink();
    const held = new HeldReleaseLink();
    const links: FakeLink[] = [failed, held];
    const manager = fakeManager(() => {
      const link = links.shift();
      if (link === undefined) throw new Error('unexpected third link');
      return link;
    });
    const first = await manager.acquire({kind: 'ssh', alias: 'first'}, '/first');
    const second = await manager.acquire({kind: 'ssh', alias: 'second'}, '/second');
    await Promise.all([first.connection.ready(), second.connection.ready()]);

    const disposing = manager[Symbol.asyncDispose]();
    let settled = false;
    void disposing.then(
      () => {
        settled = true;
      },
      () => {
        settled = true;
      },
    );
    await held.releaseStarted;

    expect(settled).toBe(false);
    expect(first.connection.phase.kind).toBe('closed');
    expect(second.connection.phase.kind).toBe('closed');
    held.finishRelease();
    expect(await rejection(disposing)).toBe(failed.failure);
    expect(failed.released).toBe(1);
    expect(held.released).toBe(1);
  });

  test('release cancels every ssh attempt and waits for every retired process to exit', async () => {
    const actions: string[] = [];
    const stale = deferredKillProcess(() => actions.push('term-stale'));
    const fresh = deferredKillProcess(() => actions.push('term-fresh'));
    let checks = 0;
    const spawn: Spawn = (_command, args) => {
      const operation = args.at(args.indexOf('-O') + 1);
      if (operation === 'check') {
        actions.push(`check-${checks}`);
        checks += 1;
        return checks === 1 ? stale.process : fresh.process;
      }
      if (operation === 'exit') {
        actions.push('exit');
        const exit = fakeProcess();
        exit.end(0);
        return exit.process;
      }
      throw new Error(`unexpected ssh operation: ${args.join(' ')}`);
    };
    const link = sshLink(spawn);
    const abandoned = new AbortController();
    const first = link.establish(false, abandoned.signal);
    abandoned.abort();
    expect((await rejection(first)).message).toContain('abandoned');
    const second = link.establish(false);

    const releasing = link.release();
    expect(actions).toEqual(['check-0', 'term-stale', 'check-1', 'term-fresh']);
    expect(actions).not.toContain('exit');
    fresh.end(null);
    expect((await rejection(second)).message).toContain('abandoned');
    expect(stale.process.running).toBe(true);
    expect(actions).not.toContain('exit');

    stale.end(null);
    await releasing;
    expect(actions).toEqual(['check-0', 'term-stale', 'check-1', 'term-fresh', 'exit']);
  });

  for (const outcome of [
    {label: 'status 255', code: 255},
    {label: 'a null signal status', code: null},
  ] as const) {
    test(`release rejects when exit returns ${outcome.label} and the master remains reachable`, async () => {
      const operations: string[] = [];
      const spawn: Spawn = (_command, args) => {
        const operation = args.at(args.indexOf('-O') + 1) ?? 'missing';
        operations.push(operation);
        const process = fakeProcess();
        process.end(operation === 'exit' ? outcome.code : 0);
        return process.process;
      };

      const error = await rejection(sshLink(spawn).release());

      expect(error).toBeInstanceOf(ConnectionError);
      expect(error.message).toContain('could not confirm teardown of 1 SSH master');
      expect(operations).toEqual(['exit', 'check']);
    });
  }

  test('release accepts a failed exit only after check confirms the master is absent', async () => {
    const operations: string[] = [];
    const spawn: Spawn = (_command, args) => {
      const operation = args.at(args.indexOf('-O') + 1) ?? 'missing';
      operations.push(operation);
      const process = fakeProcess();
      process.end(255);
      return process.process;
    };

    await sshLink(spawn).release();

    expect(operations).toEqual(['exit', 'check']);
  });

  test('release kills and rejects an exit command that stays unconfirmed past its bound', async () => {
    const clock = new FakeClock();
    const kills: string[] = [];
    const hung = deferredKillProcess(signal => kills.push(signal));
    const link = sshLink(() => hung.process, clock);

    const releasing = link.release();
    await clock.waitUntilScheduled(100);
    clock.advanceBy(200);
    const error = await rejection(releasing);

    expect(kills).toEqual(['SIGKILL']);
    expect(error).toBeInstanceOf(ConnectionError);
    expect(error.message).toContain('could not confirm teardown');
    hung.end(null);
  });

  test('abandoning one shared ssh waiter leaves the other waiter and process intact', async () => {
    const check = fakeProcess();
    const spawn: Spawn = (_command, args) => {
      const operation = args.at(args.indexOf('-O') + 1);
      if (operation === 'check') return check.process;
      if (operation === 'exit') {
        const exit = fakeProcess();
        exit.end(0);
        return exit.process;
      }
      throw new Error(`unexpected ssh operation: ${args.join(' ')}`);
    };
    const link = sshLink(spawn);
    const abandoned = new AbortController();
    const retained = new AbortController();
    const first = link.establish(false, abandoned.signal);
    const second = link.establish(false, retained.signal);

    abandoned.abort();

    expect((await rejection(first)).message).toContain('abandoned');
    expect(check.process.running).toBe(true);
    check.end(0);
    await second;
    await link.release();
  });

  test('network wake retires one shared ssh attempt before connections start a fresh one', async () => {
    const helper = new FakeHelper();
    const checks: ReturnType<typeof fakeProcess>[] = [];
    const killed: number[] = [];
    const spawn: Spawn = (_command, args) => {
      const control = args.indexOf('-O');
      const operation = control < 0 ? null : args[control + 1];
      if (operation === 'check') {
        const index = checks.length;
        const check = fakeProcess(() => killed.push(index));
        checks.push(check);
        return check.process;
      }
      if (operation === 'exit') {
        const exit = fakeProcess();
        exit.end(0);
        return exit.process;
      }
      if (args.at(-1) === 'true') throw new Error('the controlled check reports a live master');
      return helper.spawn();
    };
    const manager = fakeManager(() => sshLink(spawn));
    const replaced = (await manager.acquire(GPU, '/a')).connection;
    const release = replaced.hold();
    const firstReady = replaced.ready();
    const shown = (await manager.acquire(GPU, '/b')).connection;
    const secondReady = shown.ready();
    expect(checks).toHaveLength(1);

    manager.wake();

    expect(killed).toEqual([0]);
    expect(checks).toHaveLength(2);
    checks[1]?.end(0);
    await Promise.all([firstReady, secondReady]);
    expect(replaced.phase.kind).toBe('ready');
    expect(shown.phase.kind).toBe('ready');
    release();
    await manager[Symbol.asyncDispose]();
  });

  test('regression: releasing a link kills an in-flight ssh master attempt before its exit', async () => {
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
    const kept = (await manager.acquire(GPU, '/src/vibesys')).connection;
    await kept.ready();
    {
      // regression: closing a checkout check ended the ssh master the kept connection used.
      await using candidate = await manager.candidate(GPU, '/src/other');
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
    const firstLease = await manager.acquire(GPU, '/a');
    const first = firstLease.connection;
    await first.ready();
    const release = first.hold();
    const secondLease = await manager.acquire(GPU, '/b');
    const second = secondLease.connection;
    expect(manager.existing('ssh:gpu')).toBe(second);
    await second.ready();
    // The attached run still dials through the connection it was attached with.
    expect(first.phase.kind).toBe('ready');
    release();
    await firstLease[Symbol.asyncDispose]();
    expect(first.phase.kind).toBe('closed');
    // Nothing holds this one: replacing it closes it at once.
    const thirdLease = await manager.acquire(GPU, '/c');
    await secondLease[Symbol.asyncDispose]();
    expect(second.phase.kind).toBe('closed');
    await thirdLease[Symbol.asyncDispose]();
    await manager[Symbol.asyncDispose]();
    expect(link.helper.running).toBe(0);
  });

  test('regression: connections that open together share one ssh master', async () => {
    const helper = new FakeHelper();
    const {spawn, seen} = fakeSsh(helper);
    const manager = fakeManager(() => sshLink(spawn));
    const kept = (await manager.acquire(GPU, '/a')).connection;
    await using candidate = await manager.candidate(GPU, '/b');
    await Promise.all([kept.ready(), candidate.ready()]);
    expect(seen.masters).toBe(1);
    await manager[Symbol.asyncDispose]();
  });

  test('a link passes the uv its first bootstrap found to the next one', async () => {
    const helper = new FakeHelper();
    helper.preamble = ['uv /opt/uv/bin/uv'];
    const {spawn, seen} = fakeSsh(helper);
    const manager = fakeManager(() => sshLink(spawn));
    const connection = (await manager.acquire(GPU, '/a')).connection;
    await connection.ready();
    helper.preamble = [];
    helper.publish(record('00000000000f', 'run-f'));
    helper.host.runs.set('00000000000f', ECHO_SERVER);
    const {stream} = await connection.openRun('00000000000f', 'run-control');
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
