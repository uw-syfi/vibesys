import {describe, expect, test} from 'bun:test';
import {ConnectionError, ConnectionManager, type HostConnection} from '@vibesys/connection';
import {FakeLink, phaseReached} from '@vibesys/connection/testing';
import {FakeClock} from '@vibesys/transport/testing';

const CONFIG = {delaysMs: [500], deadlineMs: 60_000, heartbeatMs: 15_000};
const GPU = {kind: 'ssh', alias: 'gpu'} as const;
const CPU = {kind: 'ssh', alias: 'cpu'} as const;

async function rejection(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('resolved instead of rejecting');
}

async function expectAuthNeeded(connection: HostConnection): Promise<void> {
  const error = await rejection(connection.ready());
  expect(error).toBeInstanceOf(ConnectionError);
  expect((error as ConnectionError).reason).toBe('auth');
  expect(connection.phase.kind).toBe('auth-needed');
}

describe('ConnectionManager', () => {
  test('a host sign-in retries a replaced connection retained by an attached run', async () => {
    const gpu = new FakeLink();
    const cpu = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: host => (host.kind === 'ssh' && host.alias === GPU.alias ? gpu : cpu),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
      config: CONFIG,
    });

    const oldLease = await manager.acquire(GPU, '/old');
    const old = oldLease.connection;
    await old.ready();
    const releaseAttachedRun = old.hold();

    gpu.linkFault = {cause: 'auth', detail: 'Permission denied'};
    gpu.helper.channels[0]?.end(255);
    await phaseReached(old, 'auth-needed');

    await using currentLease = await manager.acquire(GPU, '/current');
    const current = currentLease.connection;
    await expectAuthNeeded(current);
    await oldLease[Symbol.asyncDispose]();
    expect(old.phase.kind).toBe('auth-needed');

    cpu.linkFault = {cause: 'auth', detail: 'Permission denied'};
    await using otherLease = await manager.acquire(CPU, '/other');
    await expectAuthNeeded(otherLease.connection);

    gpu.linkFault = null;
    const interactiveSignIn = gpu.deferEstablish();
    const connectionRetries = gpu.deferEstablish();
    const signingIn = manager.signIn(GPU);
    await interactiveSignIn.received;
    interactiveSignIn.release();
    await connectionRetries.received;

    // Both connections belonging to this host receive the user's retry intent. The other host
    // remains untouched even though it is waiting for authentication too.
    expect(old.phase.kind).toBe('linking');
    expect(current.phase.kind).toBe('linking');
    expect(otherLease.connection.phase.kind).toBe('auth-needed');
    expect(cpu.interactive).toBe(0);

    connectionRetries.release();
    await signingIn;
    await Promise.all([phaseReached(old, 'ready'), phaseReached(current, 'ready')]);

    releaseAttachedRun();
  });
});

test('additional transport owners retire before their shared host link, exactly once', async () => {
  const link = new FakeLink();
  const clock = new FakeClock();
  const manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: clock.schedule,
    log: () => {},
  });
  const lease = await manager.leaseLink({kind: 'local'});
  let retired = 0;
  manager.onDisposing(() => {
    expect(link.released).toBe(0);
    retired += 1;
  });
  await manager[Symbol.asyncDispose]();
  await manager[Symbol.asyncDispose]();
  expect(retired).toBe(1);
  await lease.release();
});
