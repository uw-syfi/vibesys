import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import type {ChannelRole} from '../state/channel-admission.js';
import {ECHO_SERVER, FakeHelper, record} from '../testing/fake-helper.js';
import {deferredKillProcess, type FakeProcess, fakeProcess} from '../testing/fake-process.js';
import {ConnectionError} from './helper-channel.js';
import {HostConnection} from './host-connection.js';
import {ChannelCapacityError, type Link, LocalLink, type Spawn, SshLink} from './link.js';
import type {SpawnedProcess} from './process.js';

const CONFIG = {delaysMs: [], deadlineMs: 60_000, heartbeatMs: 15_000};

interface FakeSshPool {
  readonly spawn: Spawn;
  readonly masters: Set<string>;
  readonly helperMasters: string[];
  readonly masterStarts: string[];
  readonly controlCalls: ControlCall[];
  readonly queuedControls: Map<string, FakeProcess[]>;
  controlCallListener: ((call: ControlCall) => void) | null;
  deferNextMaster: FakeProcess | null;
  refuseNextHelperOn: string | null;
}

interface ControlCall {
  readonly path: string;
  readonly operation: string;
}

function controlPath(args: readonly string[]): string {
  const option = args.find(argument => argument.startsWith('ControlPath='));
  if (option === undefined) throw new Error('ssh command has no control path');
  return option.slice('ControlPath='.length);
}

function controlOperation(pool: FakeSshPool, path: string, operation: string): SpawnedProcess {
  const call = {path, operation};
  pool.controlCalls.push(call);
  pool.controlCallListener?.(call);
  const key = `${path}:${operation}`;
  const queued = pool.queuedControls.get(key)?.shift();
  if (queued !== undefined) {
    void queued.process.exit.then(code => {
      if (operation === 'exit' && code === 0) pool.masters.delete(path);
    });
    return queued.process;
  }
  const process = fakeProcess();
  if (operation === 'exit') pool.masters.delete(path);
  process.end(operation === 'check' && !pool.masters.has(path) ? 255 : 0);
  return process.process;
}

function nextControlCall(pool: FakeSshPool): Promise<ControlCall> {
  if (pool.controlCallListener !== null)
    throw new Error('a control-call observer is already active');
  return new Promise(resolve => {
    pool.controlCallListener = call => {
      pool.controlCallListener = null;
      resolve(call);
    };
  });
}

function queueControl(
  pool: FakeSshPool,
  path: string,
  operation: 'exit' | 'check',
  process: FakeProcess,
): void {
  const key = `${path}:${operation}`;
  const queue = pool.queuedControls.get(key) ?? [];
  queue.push(process);
  pool.queuedControls.set(key, queue);
}

function startMaster(pool: FakeSshPool, path: string): SpawnedProcess {
  pool.masterStarts.push(path);
  const deferred = pool.deferNextMaster;
  pool.deferNextMaster = null;
  if (deferred !== null) {
    void deferred.process.exit.then(code => {
      if (code === 0) pool.masters.add(path);
    });
    return deferred.process;
  }
  pool.masters.add(path);
  const process = fakeProcess();
  process.end(0);
  return process.process;
}

function startHelper(pool: FakeSshPool, helper: FakeHelper, path: string): SpawnedProcess {
  pool.helperMasters.push(path);
  if (pool.refuseNextHelperOn !== path) return helper.spawn();
  pool.refuseNextHelperOn = null;
  const process = fakeProcess();
  process.end(
    255,
    '',
    'mux_client_request_session: session request failed: Session open refused by peer\n',
  );
  return process.process;
}

function fakeSshPool(helper: FakeHelper): FakeSshPool {
  const pool: FakeSshPool = {
    masters: new Set(),
    helperMasters: [],
    masterStarts: [],
    controlCalls: [],
    queuedControls: new Map(),
    controlCallListener: null,
    deferNextMaster: null,
    refuseNextHelperOn: null,
    spawn: (_command, args) => {
      const path = controlPath(args);
      const operationIndex = args.indexOf('-O');
      const operation = operationIndex < 0 ? null : (args[operationIndex + 1] ?? null);
      if (operation !== null) return controlOperation(pool, path, operation);
      if (args.at(-1) === 'true') return startMaster(pool, path);
      return startHelper(pool, helper, path);
    },
  };
  return pool;
}

function connection(link: SshLink, clock = new FakeClock()): HostConnection {
  return new HostConnection({
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
  });
}

function link(
  pool: FakeSshPool,
  options: {capacityPerMaster: number; reservedControlSlots: number; maxMasters: number},
  clock = new FakeClock(),
): SshLink {
  return new SshLink({
    alias: 'gpu',
    controlPath: '/tmp/cm-%C',
    askpass: '/askpass',
    spawn: pool.spawn,
    scheduleTimeout: clock.scheduleTimeout,
    retirementCommandTimeoutMs: 100,
    ...options,
  });
}

async function openProcess(
  ssh: SshLink,
  role: ChannelRole,
  signal = new AbortController().signal,
): Promise<SpawnedProcess> {
  return ssh.withHelper('/src/vibesys', role, signal, process => {
    process.stdout.resume();
    process.stderr.resume();
    return Promise.resolve(process);
  });
}

async function stopProcess(process: SpawnedProcess): Promise<void> {
  process.kill('SIGTERM');
  await process.exit;
}

async function rejection(promise: Promise<unknown>): Promise<Error> {
  return promise.then(
    () => {
      throw new Error('resolved instead of rejecting');
    },
    error => error as Error,
  );
}

interface LinkHarness {
  readonly link: Link;
  readonly helperStarts: () => number;
}

function localHarness(): LinkHarness {
  const helpers: FakeProcess[] = [];
  const local = new LocalLink(() => {
    const process = fakeProcess();
    helpers.push(process);
    return process.process;
  });
  return {link: local, helperStarts: () => helpers.length};
}

function sshHarness(): LinkHarness {
  const helper = new FakeHelper();
  const pool = fakeSshPool(helper);
  return {
    link: link(pool, {capacityPerMaster: 4, reservedControlSlots: 1, maxMasters: 2}),
    helperStarts: () => pool.helperMasters.length,
  };
}

function linkContract(name: string, create: () => LinkHarness): void {
  describe(`role-aware Link contract (${name})`, () => {
    test('accepts every closed role without imposing local-only capacity', async () => {
      const harness = create();
      await harness.link.establish(false);
      const roles: readonly ChannelRole[] = ['host-control', 'run-control', 'subscribe', 'chat'];
      for (const role of roles) {
        const process = await harness.link.withHelper(
          '/src/vibesys',
          role,
          new AbortController().signal,
          candidate => Promise.resolve(candidate),
        );
        process.stdout.resume();
        process.stderr.resume();
        process.kill('SIGTERM');
        await process.exit;
      }
      expect(harness.helperStarts()).toBe(roles.length);
      await harness.link.release();
    });

    test('a pre-cancelled acquisition starts no helper', async () => {
      const harness = create();
      await harness.link.establish(false);
      const before = harness.helperStarts();
      const cancelled = new AbortController();
      cancelled.abort(new DOMException('page closed', 'AbortError'));

      const error = await rejection(
        harness.link.withHelper('/src/vibesys', 'chat', cancelled.signal, process =>
          Promise.resolve(process),
        ),
      );

      expect(error.name).toBe('AbortError');
      expect(harness.helperStarts()).toBe(before);
      await harness.link.release();
    });

    test('release fences every later acquisition', async () => {
      const harness = create();
      await harness.link.establish(false);
      await harness.link.release();

      await expect(
        harness.link.withHelper(
          '/src/vibesys',
          'run-control',
          new AbortController().signal,
          process => Promise.resolve(process),
        ),
      ).rejects.toBeInstanceOf(Error);
      expect(harness.helperStarts()).toBe(0);
    });
  });
}

linkContract('local', localHarness);
linkContract('fake SSH', sshHarness);

describe('role-aware SSH link pool', () => {
  test('a base-master authentication failure remains a host authentication failure', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const failedBase = fakeProcess();
    pool.deferNextMaster = failedBase;
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
    const establishing = ssh.establish(false);
    failedBase.end(255, '', 'Permission denied (publickey,password).\n');

    const error = await rejection(establishing);
    await ssh.release();

    expect(error).toBeInstanceOf(ConnectionError);
    expect((error as ConnectionError).reason).toBe('auth');
  });

  test('admits a follower when the paused spill creator cancels and retains its master', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const spill = fakeProcess();
    pool.deferNextMaster = spill;
    const creatorAbort = new AbortController();

    const creator = openProcess(ssh, 'chat', creatorAbort.signal);
    const subscriber = openProcess(ssh, 'subscribe');
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    creatorAbort.abort(new DOMException('creator closed', 'AbortError'));
    expect((await rejection(creator)).name).toBe('AbortError');

    spill.end(0);
    const subscribed = await subscriber;

    expect(pool.helperMasters).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(ssh.diagnostics()).toMatchObject({
      masterCount: 2,
      active: {'host-control': 0, 'run-control': 0, subscribe: 1, chat: 1},
    });
    expect(pool.masters.has('/tmp/cm-%C.1')).toBe(true);
    await Promise.all([stopProcess(base), stopProcess(subscribed)]);
    await ssh.release();
  });

  test('grants queued subscribe ahead of the chat that opened a paused spill', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const spill = fakeProcess();
    pool.deferNextMaster = spill;

    const chat = openProcess(ssh, 'chat');
    const subscriber = openProcess(ssh, 'subscribe');
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    spill.end(0);
    const subscribed = await subscriber;
    const chatError = await rejection(chat);

    expect(chatError).toBeInstanceOf(ChannelCapacityError);
    expect((chatError as ChannelCapacityError).role).toBe('chat');
    expect(pool.helperMasters).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(ssh.diagnostics().active).toEqual({
      'host-control': 0,
      'run-control': 0,
      subscribe: 1,
      chat: 1,
    });
    await Promise.all([stopProcess(base), stopProcess(subscribed)]);
    await ssh.release();
  });

  test('fails every spill waiter once and retries after confirming the failed spill is absent', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const failedSpill = fakeProcess();
    const confirmedAbsent = fakeProcess();
    pool.deferNextMaster = failedSpill;
    queueControl(pool, '/tmp/cm-%C.1', 'exit', confirmedAbsent);
    const chat = openProcess(ssh, 'chat');
    const subscriber = openProcess(ssh, 'subscribe');

    failedSpill.end(255, '', 'connection refused\n');
    const [chatFailure, subscriberFailure] = await Promise.all([
      rejection(chat),
      rejection(subscriber),
    ]);
    expect(chatFailure).toBeInstanceOf(ChannelCapacityError);
    expect((chatFailure as ChannelCapacityError).role).toBe('chat');
    expect(subscriberFailure).toBeInstanceOf(ChannelCapacityError);
    expect((subscriberFailure as ChannelCapacityError).role).toBe('subscribe');
    expect(ssh.diagnostics().masterCount).toBe(2);
    expect(pool.controlCalls.at(-1)).toEqual({
      path: '/tmp/cm-%C.1',
      operation: 'exit',
    });

    const retrySpill = fakeProcess();
    pool.deferNextMaster = retrySpill;
    const retry = openProcess(ssh, 'subscribe');
    expect(pool.helperMasters).toEqual(['/tmp/cm-%C']);
    retrySpill.end(0);
    confirmedAbsent.end(0);
    const retried = await retry;
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1', '/tmp/cm-%C.2']);
    expect(pool.helperMasters).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.2']);

    await Promise.all([stopProcess(base), stopProcess(retried)]);
    await ssh.release();
  });

  test('reconciles a reachable spill after its creation command fails before reusing it', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const spillPath = '/tmp/cm-%C.1';
    const failedSpill = fakeProcess();
    const refusedExit = fakeProcess();
    const reachableCheck = fakeProcess();
    pool.deferNextMaster = failedSpill;
    pool.masters.add(spillPath);
    queueControl(pool, spillPath, 'exit', refusedExit);
    queueControl(pool, spillPath, 'check', reachableCheck);
    const opening = openProcess(ssh, 'chat');

    failedSpill.end(1, '', 'remote command failed\n');
    const failure = await rejection(opening);

    expect(failure).toBeInstanceOf(ChannelCapacityError);
    expect((failure as ChannelCapacityError).role).toBe('chat');
    expect(ssh.diagnostics().masterCount).toBe(2);
    expect(pool.controlCalls.at(-1)).toEqual({path: spillPath, operation: 'exit'});

    const retry = openProcess(ssh, 'chat');
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', spillPath]);
    expect(pool.helperMasters).toEqual(['/tmp/cm-%C']);

    const checking = nextControlCall(pool);
    refusedExit.end(255);
    expect(await checking).toEqual({path: spillPath, operation: 'check'});
    expect(pool.helperMasters).toEqual(['/tmp/cm-%C']);
    reachableCheck.end(0);
    const reused = await retry;

    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', spillPath]);
    expect(pool.helperMasters.at(-1)).toBe(spillPath);
    expect(pool.masters).toEqual(new Set(['/tmp/cm-%C', spillPath]));

    await Promise.all([stopProcess(base), stopProcess(reused)]);
    await ssh.release();
  });

  test('a spill authentication failure leaves the established host connection healthy', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000008', 'run-8'));
    helper.host.runs.set('000000000008', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
    const host = connection(ssh);
    await host.ready();
    const failedSpill = fakeProcess();
    pool.deferNextMaster = failedSpill;
    const opening = host.openRun('000000000008', 'subscribe');
    failedSpill.end(255, '', 'Permission denied (publickey,password).\n');

    const error = await rejection(opening);
    const phase = host.phase.kind;
    await host[Symbol.asyncDispose]();
    await ssh.release();

    expect(error).toBeInstanceOf(ChannelCapacityError);
    expect((error as ChannelCapacityError).role).toBe('subscribe');
    expect(phase).toBe('ready');
  });

  test('reserves base-master control capacity and spills passive channels deterministically', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000001', 'run-1'));
    helper.host.runs.set('000000000001', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 3, reservedControlSlots: 1, maxMasters: 2});
    await using host = connection(ssh);
    await host.ready();

    const subscribe = await host.openRun('000000000001', 'subscribe');
    const chat = await host.openRun('000000000001', 'chat');

    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(pool.helperMasters).toEqual(['/tmp/cm-%C', '/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(ssh.diagnostics()).toEqual({
      masterCount: 2,
      opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
      active: {'host-control': 1, 'run-control': 0, subscribe: 1, chat: 1},
      masters: [
        {
          opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
          active: {'host-control': 1, 'run-control': 0, subscribe: 1, chat: 0},
        },
        {
          opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
          active: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 1},
        },
      ],
    });
    subscribe.stream.destroy();
    chat.stream.destroy();
  });

  test('keeps the reserved slot available to run control when passive capacity is full', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000002', 'run-2'));
    helper.host.runs.set('000000000002', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 3, reservedControlSlots: 1, maxMasters: 2});
    await using host = connection(ssh);
    await host.ready();

    const subscribe = await host.openRun('000000000002', 'subscribe');
    const control = await host.openRun('000000000002', 'run-control');

    expect(pool.masterStarts).toEqual(['/tmp/cm-%C']);
    expect(ssh.diagnostics().active).toEqual({
      'host-control': 1,
      'run-control': 1,
      subscribe: 1,
      chat: 0,
    });
    subscribe.stream.destroy();
    control.stream.destroy();
  });

  test('returns typed passive capacity without failing the healthy host connection', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000003', 'run-3'));
    helper.host.runs.set('000000000003', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 3, reservedControlSlots: 1, maxMasters: 1});
    await using host = connection(ssh);
    await host.ready();
    const subscribe = await host.openRun('000000000003', 'subscribe');

    const error = await rejection(host.openRun('000000000003', 'chat'));

    expect(error).toBeInstanceOf(ChannelCapacityError);
    expect(host.phase.kind).toBe('ready');
    expect(helper.running).toBe(2);
    subscribe.stream.destroy();
  });

  test('retries one pre-handshake session refusal on a spillover master', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000004', 'run-4'));
    helper.host.runs.set('000000000004', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 4, reservedControlSlots: 1, maxMasters: 2});
    await using host = connection(ssh);
    await host.ready();
    pool.refuseNextHelperOn = '/tmp/cm-%C';

    const opened = await host.openRun('000000000004', 'run-control');

    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(pool.helperMasters.slice(-2)).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(host.phase.kind).toBe('ready');
    opened.stream.destroy();
  });

  test('cancels queued admission without spawning a late helper', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000005', 'run-5'));
    helper.host.runs.set('000000000005', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 3, reservedControlSlots: 1, maxMasters: 2});
    await using host = connection(ssh);
    await host.ready();
    const subscribe = await host.openRun('000000000005', 'subscribe');
    const spill = fakeProcess();
    pool.deferNextMaster = spill;
    const abort = new AbortController();
    const opening = host.openRun('000000000005', 'chat', abort.signal);
    await Promise.resolve();
    abort.abort(new DOMException('page closed', 'AbortError'));

    expect((await rejection(opening)).name).toBe('AbortError');
    expect(pool.helperMasters).toHaveLength(2);
    spill.end(0);
    await spill.process.exit;
    await Promise.resolve();
    expect(pool.helperMasters).toHaveLength(2);
    expect(host.phase.kind).toBe('ready');
    subscribe.stream.destroy();
  });

  test('shares one paused spillover creation across concurrent demand', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000006', 'run-6'));
    helper.host.runs.set('000000000006', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 3, reservedControlSlots: 1, maxMasters: 2});
    await using host = connection(ssh);
    await host.ready();
    const subscribe = await host.openRun('000000000006', 'subscribe');
    const spill = fakeProcess();
    pool.deferNextMaster = spill;

    const first = host.openRun('000000000006', 'chat');
    const second = host.openRun('000000000006', 'chat');
    await Promise.resolve();
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);

    spill.end(0);
    const [firstOpened, secondOpened] = await Promise.all([first, second]);
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(pool.helperMasters.slice(-2)).toEqual(['/tmp/cm-%C.1', '/tmp/cm-%C.1']);
    subscribe.stream.destroy();
    firstOpened.stream.destroy();
    secondOpened.stream.destroy();
  });

  for (const outcome of [
    {label: 'exit status 255', code: 255},
    {label: 'a null signal exit', code: null},
  ] as const) {
    test(`reconciles ${outcome.label} and reuses the reachable spill for queued demand`, async () => {
      const helper = new FakeHelper();
      const pool = fakeSshPool(helper);
      const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2});
      await ssh.establish(false);
      const base = await openProcess(ssh, 'chat');
      const spill = await openProcess(ssh, 'chat');
      const failedExit = fakeProcess();
      failedExit.end(outcome.code);
      queueControl(pool, '/tmp/cm-%C.1', 'exit', failedExit);

      await stopProcess(spill);
      const reused = await openProcess(ssh, 'chat');

      expect(pool.controlCalls.slice(-2)).toEqual([
        {path: '/tmp/cm-%C.1', operation: 'exit'},
        {path: '/tmp/cm-%C.1', operation: 'check'},
      ]);
      expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
      expect(pool.helperMasters.at(-1)).toBe('/tmp/cm-%C.1');
      expect(pool.masters).toEqual(new Set(['/tmp/cm-%C', '/tmp/cm-%C.1']));
      expect(ssh.diagnostics().masterCount).toBe(2);

      await Promise.all([stopProcess(base), stopProcess(reused)]);
      await ssh.release();
    });
  }

  test('bounds an unconfirmed hung retirement and terminally settles demand queued behind it', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const clock = new FakeClock();
    const ssh = link(pool, {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2}, clock);
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const spill = await openProcess(ssh, 'chat');
    const kills: string[] = [];
    const hungExit = deferredKillProcess(signal => kills.push(signal));
    queueControl(pool, '/tmp/cm-%C.1', 'exit', hungExit);

    await stopProcess(spill);
    const waiting = openProcess(ssh, 'chat');
    await clock.waitUntilScheduled(100);
    clock.advanceBy(200);
    const error = await rejection(waiting);

    expect(kills).toEqual(['SIGKILL']);
    expect(error).toBeInstanceOf(ChannelCapacityError);
    expect(pool.controlCalls.at(-1)).toEqual({path: '/tmp/cm-%C.1', operation: 'exit'});
    expect(pool.masterStarts).toEqual(['/tmp/cm-%C', '/tmp/cm-%C.1']);
    expect(ssh.diagnostics().masterCount).toBe(2);

    hungExit.end(null);
    await hungExit.process.exit;
    await Promise.resolve();
    await stopProcess(base);
    await ssh.release();
  });

  test('final release rejects partial base-and-spill teardown with truthful diagnostics', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 1, reservedControlSlots: 0, maxMasters: 3});
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const firstSpill = await openProcess(ssh, 'chat');
    const secondSpill = await openProcess(ssh, 'chat');

    const refusedExit = fakeProcess();
    refusedExit.end(255);
    queueControl(pool, '/tmp/cm-%C.1', 'exit', refusedExit);
    const signalledExit = fakeProcess();
    signalledExit.end(null);
    queueControl(pool, '/tmp/cm-%C.2', 'exit', signalledExit);
    const unknownCheck = fakeProcess();
    unknownCheck.end(null);
    queueControl(pool, '/tmp/cm-%C.2', 'check', unknownCheck);

    const releasing = ssh.release();
    await Promise.all([stopProcess(base), stopProcess(firstSpill), stopProcess(secondSpill)]);
    const error = await rejection(releasing);

    expect(error).toBeInstanceOf(ConnectionError);
    expect(error.message).toContain('could not confirm teardown of 2 SSH masters');
    expect(pool.masters).toEqual(new Set(['/tmp/cm-%C.1', '/tmp/cm-%C.2']));
    expect(ssh.diagnostics()).toEqual({
      masterCount: 2,
      opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
      active: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
      masters: [
        {
          opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
          active: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
        },
        {
          opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
          active: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
        },
      ],
    });
  });

  test('successful final release removes every master from diagnostics', async () => {
    const helper = new FakeHelper();
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 1, reservedControlSlots: 0, maxMasters: 2});
    await ssh.establish(false);
    const base = await openProcess(ssh, 'chat');
    const spill = await openProcess(ssh, 'chat');

    const releasing = ssh.release();
    await Promise.all([stopProcess(base), stopProcess(spill)]);
    await releasing;

    expect(pool.masters).toEqual(new Set());
    expect(ssh.diagnostics()).toEqual({
      masterCount: 0,
      opening: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
      active: {'host-control': 0, 'run-control': 0, subscribe: 0, chat: 0},
      masters: [],
    });
  });

  test('release kills a paused spillover creation and starts no late helper', async () => {
    const helper = new FakeHelper();
    helper.publish(record('000000000007', 'run-7'));
    helper.host.runs.set('000000000007', ECHO_SERVER);
    const pool = fakeSshPool(helper);
    const ssh = link(pool, {capacityPerMaster: 3, reservedControlSlots: 1, maxMasters: 2});
    const host = connection(ssh);
    await host.ready();
    const subscribe = await host.openRun('000000000007', 'subscribe');
    const spill = fakeProcess();
    pool.deferNextMaster = spill;
    const opening = host.openRun('000000000007', 'chat');
    const waiting = host.openRun('000000000007', 'subscribe');
    await Promise.resolve();
    expect(spill.process.running).toBe(true);

    const closingHost = host[Symbol.asyncDispose]();
    const releasingLink = ssh.release();
    await Promise.all([closingHost, releasingLink]);

    expect(spill.process.running).toBe(false);
    expect(pool.helperMasters).toHaveLength(2);
    const [openingError, waitingError] = await Promise.all([
      rejection(opening),
      rejection(waiting),
    ]);
    expect(openingError).toBeInstanceOf(Error);
    expect(waitingError).toBeInstanceOf(Error);
    subscribe.stream.destroy();
  });
});
