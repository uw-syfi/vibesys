import {describe, expect, test} from 'bun:test';
import {
  type ConnectionLease,
  ConnectionManager,
  DEFAULT_CONNECTION_CONFIG,
  HostConnection,
} from '@vibesys/connection';
import {FakeLink, phaseReached, record} from '@vibesys/connection/testing';
import type {InstanceList} from '@vibesys/contracts/host-helper';
import type {ConnectionStatus} from '@vibesys/hosts';
import type {MainToPage} from '@vibesys/ipc';
import {exactResumeRunId} from '@vibesys/runs';
import type {StoredRecentRun} from '@vibesys/settings';
import {FakeClock} from '@vibesys/transport/testing';
import type {RelayPort} from './relay.js';
import {RunControl} from './run-control.js';

const HOST = {kind: 'local'} as const;
const RUN_A = '00000000000a';
const RUN_B = '00000000000b';

interface Gate {
  readonly received: Promise<void>;
  readonly wait: Promise<void>;
  receive(): void;
  release(): void;
}

function gate(): Gate {
  let receive = (): void => {};
  let release = (): void => {};
  return {
    received: new Promise<void>(resolve => {
      receive = resolve;
    }),
    wait: new Promise<void>(resolve => {
      release = resolve;
    }),
    receive: () => receive(),
    release: () => release(),
  };
}

function lease(connection: HostConnection): ConnectionLease {
  const release = connection.hold();
  let released = false;
  return {
    connection,
    async [Symbol.asyncDispose]() {
      if (released) return;
      released = true;
      release();
    },
  };
}

class FakeLog {
  readonly messages: string[] = [];
  readonly #waiters: {readonly text: string; readonly resolve: () => void}[] = [];

  write(message: string): void {
    this.messages.push(message);
    for (let index = this.#waiters.length - 1; index >= 0; index -= 1) {
      const waiter = this.#waiters[index];
      if (waiter === undefined || !message.includes(waiter.text)) continue;
      this.#waiters.splice(index, 1);
      waiter.resolve();
    }
  }

  waitFor(text: string): Promise<void> {
    if (this.messages.some(message => message.includes(text))) return Promise.resolve();
    return new Promise<void>(resolve => this.#waiters.push({text, resolve}));
  }
}

/** One run-page connection, controlled synchronously by the test. */
class FakeRelayPort implements RelayPort {
  readonly posted: MainToPage[] = [];
  closed = false;
  #close: () => void = () => {};

  post(message: MainToPage): void {
    this.posted.push(message);
  }
  onMessage(_listener: (data: unknown) => void): void {}
  onClose(listener: () => void): void {
    this.#close = listener;
  }
  close(): void {
    this.closed = true;
  }
  disconnect(): void {
    this.#close();
  }
}

/** The recent-runs writer, including a gate before its next guarded commit. */
class FakeRecentRuns {
  readonly runs: StoredRecentRun[] = [];
  #queue: Promise<void> = Promise.resolve();
  #next: Gate | null = null;

  deferNext(): Gate {
    if (this.#next !== null) throw new Error('a recent update is already deferred');
    this.#next = gate();
    return this.#next;
  }

  remember(run: StoredRecentRun, current: () => boolean): Promise<void> {
    const write = async (): Promise<void> => {
      const next = [...this.runs, run];
      const pending = this.#next;
      this.#next = null;
      if (pending !== null) {
        // Like SettingsStore: stage the next file, then wait at the guarded commit boundary.
        pending.receive();
        await pending.wait;
      }
      if (!current()) return;
      this.runs.splice(0, this.runs.length, ...next);
    };
    const next = this.#queue.then(write);
    this.#queue = next.catch(() => undefined);
    return next;
  }
}

/** A RunControl over one FakeHelper host, with the pages it opened and the runs it remembered. */
function world(
  options: {
    readonly failConnectionCall?: number;
    readonly deferConnectionCall?: number;
    readonly deferConnectionCalls?: readonly number[];
  } = {},
) {
  const link = new FakeLink();
  link.helper.host.projects.set('/p', ['spsc']);
  const connection = new HostConnection({
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: DEFAULT_CONNECTION_CONFIG,
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
  const recents = new FakeRecentRuns();
  const pages = {opened: 0, closed: 0, woken: 0, confirms: 0};
  const log = new FakeLog();
  const calls = {connections: 0};
  const deferredConnections = new Map(
    [
      ...(options.deferConnectionCall === undefined ? [] : [options.deferConnectionCall]),
      ...(options.deferConnectionCalls ?? []),
    ].map(call => [call, gate()] as const),
  );
  const deferredConnection =
    options.deferConnectionCall === undefined
      ? null
      : (deferredConnections.get(options.deferConnectionCall) ?? null);
  const runs = new RunControl({
    connection: async () => {
      calls.connections += 1;
      const call = calls.connections;
      const deferred = deferredConnections.get(call);
      if (deferred !== undefined) {
        deferred.receive();
        await deferred.wait;
      }
      if (call === options.failConnectionCall) throw new Error('the cleanup connection failed');
      return lease(connection);
    },
    openPage: () => {
      pages.opened += 1;
      return {wake: () => (pages.woken += 1), close: () => (pages.closed += 1)};
    },
    confirm: async () => {
      pages.confirms += 1;
      return true;
    },
    remember: (run, current) => recents.remember(run, current),
    changed: () => {},
    log: message => log.write(message),
    now: () => 7,
  });
  return {
    link,
    connection,
    runs,
    remembered: recents.runs,
    recents,
    pages,
    logs: log.messages,
    waitForLog: (text: string) => log.waitFor(text),
    calls,
    deferredConnection,
    deferredConnectionAt: (call: number) => deferredConnections.get(call) ?? null,
  };
}

/** Resolve once the attached run's connection reads `kind`. */
async function status(runs: RunControl, kind: ConnectionStatus['kind']): Promise<void> {
  for (let turn = 0; turn < 200 && runs.attached()?.connection.kind !== kind; turn += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
  expect(runs.attached()?.connection.kind).toBe(kind);
}

/** Perform `change`, then resolve once the connection has delivered its resulting listing. */
async function observeListing(
  connection: HostConnection,
  expected: (listing: InstanceList) => boolean,
  change: () => void,
): Promise<void> {
  let release = (): void => {};
  const observed = new Promise<void>(resolve => {
    release = connection.watchInstances(listing => {
      if (expected(listing)) resolve();
    });
  });
  change();
  await observed;
  release();
}

describe('RunControl over the FakeHelper', () => {
  test('regression: a start while the host listing is already held binds the run, not an ended one', async () => {
    const w = world();
    // The connection already holds a listing from before the start published its record.
    await new Promise<void>(resolve => w.connection.watchInstances(() => resolve()));
    expect(await w.runs.start(HOST, '/p', 'spsc', [])).toBe('started');
    const attached = w.runs.attached();
    expect(attached?.connection.kind).not.toBe('run-ended');
    // The run's identity is its record's, recorded when the start answered.
    const live = w.link.helper.host.instances[0];
    expect(attached?.runId).toBe(live?.run_id);
    expect(w.remembered).toEqual([
      {
        host: 'local',
        project: '/p',
        task: 'spsc',
        runId: live?.run_id ?? '',
        instanceId: live?.id ?? '',
        attachedAt: 7,
      },
    ]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a run whose server ends is shown ended, from the pushed registry', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000a', 'run-a', '/p'));
    await w.runs.attach(HOST, RUN_A);
    await status(w.runs, 'connected');
    w.link.helper.end('00000000000a');
    await status(w.runs, 'run-ended');
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    ['unverified', 'could not confirm whether run'],
    ['unreadable', 'cannot be read by this VibeSys version'],
  ] as const)('attaching to an %s instance reports uncertainty instead of saying it ended', async (uncertain, message) => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    if (uncertain === 'unverified') w.link.helper.unverify(RUN_A);
    else w.link.helper.makeUnreadable(RUN_A);

    await expect(w.runs.attach(HOST, RUN_A)).rejects.toThrow(message);
    expect(w.runs.attached()).toBeNull();
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    'unverified',
    'unreadable',
  ] as const)('an attached run survives live → %s → live registry observations', async uncertain => {
    const w = world();
    const live = record(RUN_A, 'run-a', '/p');
    w.link.helper.publish(live);
    await w.runs.attach(HOST, RUN_A);
    expect(w.runs.attached()?.connection.kind).toBe('connected');

    await observeListing(
      w.connection,
      listing =>
        uncertain === 'unverified'
          ? (listing.unverified ?? []).includes(RUN_A)
          : (listing.unreadable ?? []).some(entry => entry.id === RUN_A),
      () => {
        if (uncertain === 'unverified') w.link.helper.unverify(RUN_A);
        else w.link.helper.makeUnreadable(RUN_A);
      },
    );
    expect(w.runs.attached()?.connection.kind).toBe('connected');

    await observeListing(
      w.connection,
      listing => listing.instances.some(entry => entry.id === RUN_A),
      () => w.link.helper.publish(live),
    );
    expect(w.runs.attached()?.connection.kind).toBe('connected');
    await w.connection[Symbol.asyncDispose]();
  });

  test('a resume of a run another server drives attaches to that server, under the same run id', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000b', 'run-b', '/p'));
    await w.runs.resume(HOST, '/p', exactResumeRunId('run-b'));
    expect(w.runs.attached()).toMatchObject({instanceId: '00000000000b', runId: 'run-b'});
    expect(w.link.helper.host.instances).toHaveLength(1);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a newer attach remains selected when an older attach answers last', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.link.helper.deferNext('instances.list');

    const older = w.runs.attach(HOST, RUN_A);
    await delayed.received;
    await w.runs.attach(HOST, RUN_B);
    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    delayed.release();
    await older;

    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.pages).toMatchObject({opened: 1, closed: 0});
    expect(w.remembered.map(run => run.runId)).toEqual(['run-b']);
    expect(w.link.helper.stops).toEqual([]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a winning attach replaces the previous binding without invalidating itself', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));

    await w.runs.attach(HOST, RUN_A);
    await w.runs.attach(HOST, RUN_B);

    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.pages).toMatchObject({opened: 2, closed: 1});
    expect(w.remembered.map(run => run.runId)).toEqual(['run-a', 'run-b']);
    await w.connection[Symbol.asyncDispose]();
  });

  test('an older attach stays superseded when the newer attach fails', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    const delayed = w.link.helper.deferNext('instances.list');

    const older = w.runs.attach(HOST, RUN_A);
    await delayed.received;
    await expect(w.runs.attach(HOST, RUN_B)).rejects.toThrow(`no live run ${RUN_B}`);
    delayed.release();
    await older;

    expect(w.runs.attached()).toBeNull();
    expect(w.pages.opened).toBe(0);
    expect(w.remembered).toEqual([]);
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    [
      'start',
      async (runs: RunControl) => {
        expect(await runs.start(HOST, '/p', 'spsc', [])).toBe('cancelled');
      },
    ],
    ['resume', (runs: RunControl) => runs.resume(HOST, '/p', exactResumeRunId('run-a'))],
  ] as const)('a superseded %s stops its newly created run once without replacing the newer attach', async (_kind, launch) => {
    const w = world();
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.link.helper.deferNext('run.start');

    const older = launch(w.runs);
    await delayed.received;
    await w.runs.attach(HOST, RUN_B);
    delayed.release();
    await older;

    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.pages).toMatchObject({opened: 1, closed: 0, confirms: 0});
    expect(w.link.helper.stops).toEqual([{instanceId: '000000000001', force: false}]);
    expect(w.link.helper.host.instances.map(entry => entry.id)).toEqual([RUN_B]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a stale start reply does not stop the same server adopted by the newer intent', async () => {
    const w = world();
    const delayed = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', []);
    // The helper has already published the serving record; only its run.start reply is held.
    await delayed.received;
    const launched = w.link.helper.host.instances[0];
    expect(launched).toBeDefined();
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    await w.runs.attach(HOST, launched.id);
    delayed.release();
    expect(await older).toBe('cancelled');

    expect(w.runs.attached()?.instanceId).toBe(launched.id);
    expect(w.link.helper.stops).toEqual([]);
    expect(w.link.helper.host.instances).toEqual([launched]);
    expect(w.pages).toMatchObject({opened: 1, closed: 0, confirms: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('a stale start reply does not stop its server while a newer attach to it is in flight', async () => {
    const w = world();
    const delayedStart = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', []);
    await delayedStart.received;
    const launched = w.link.helper.host.instances[0];
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    const delayedAttach = w.link.helper.deferNext('instances.list');
    const adopting = w.runs.attach(HOST, launched.id);
    await delayedAttach.received;

    delayedStart.release();
    await w.waitForLog(`waiting for the newer selection of run ${launched.id}`);
    expect(w.runs.attached()).toBeNull();
    expect(w.link.helper.stops).toEqual([]);
    expect(w.link.helper.host.instances).toEqual([launched]);

    delayedAttach.release();
    const [, outcome] = await Promise.all([adopting, older]);
    expect(outcome).toBe('cancelled');
    expect(w.runs.attached()?.instanceId).toBe(launched.id);
    expect(w.pages).toMatchObject({opened: 1, closed: 0, confirms: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('a failed same-target attach releases one pending cleanup for the stale start', async () => {
    const w = world({deferConnectionCall: 2, failConnectionCall: 2});
    const delayedStart = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', []);
    await delayedStart.received;
    const launched = w.link.helper.host.instances[0];
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    const adopting = w.runs.attach(HOST, launched.id);
    const heldAttach = w.deferredConnection;
    if (heldAttach === null) throw new Error('attach connection was not deferred');
    await heldAttach.received;

    delayedStart.release();
    await w.waitForLog(`waiting for the newer selection of run ${launched.id}`);
    expect(w.link.helper.stops).toEqual([]);
    heldAttach.release();
    await expect(adopting).rejects.toThrow('the cleanup connection failed');
    expect(await older).toBe('cancelled');

    expect(w.link.helper.stops).toEqual([{instanceId: launched.id, force: false}]);
    expect(w.link.helper.host.instances).toEqual([]);
    expect(w.runs.attached()).toBeNull();
    await w.connection[Symbol.asyncDispose]();
  });

  test('a target adopted while stale cleanup waits is rechecked before the stop write', async () => {
    const w = world({deferConnectionCall: 3});
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayedStart = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', []);
    await delayedStart.received;
    const launched = w.link.helper.host.instances.find(instance => instance.id !== RUN_B);
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    await w.runs.attach(HOST, RUN_B);
    delayedStart.release();
    const cleanup = w.deferredConnection;
    if (cleanup === null) throw new Error('cleanup connection was not deferred');
    await cleanup.received;

    const delayedAttach = w.link.helper.deferNext('instances.list');
    const adopting = w.runs.attach(HOST, launched.id);
    await delayedAttach.received;
    cleanup.release();
    await w.waitForLog(`waiting for the newer selection of run ${launched.id}`);
    expect(w.link.helper.stops).toEqual([]);

    delayedAttach.release();
    const [, outcome] = await Promise.all([adopting, older]);
    expect(outcome).toBe('cancelled');
    expect(w.runs.attached()?.instanceId).toBe(launched.id);
    expect(w.link.helper.host.instances.map(instance => instance.id)).toEqual([RUN_B, launched.id]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a withdrawn stale cleanup stops once when its new same-target attach fails', async () => {
    const w = world({deferConnectionCalls: [3, 4], failConnectionCall: 4});
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayedStart = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', []);
    await delayedStart.received;
    const launched = w.link.helper.host.instances.find(instance => instance.id !== RUN_B);
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    await w.runs.attach(HOST, RUN_B);
    delayedStart.release();
    const cleanup = w.deferredConnectionAt(3);
    if (cleanup === null) throw new Error('cleanup connection was not deferred');
    await cleanup.received;

    const adopting = w.runs.attach(HOST, launched.id);
    const heldAttach = w.deferredConnectionAt(4);
    if (heldAttach === null) throw new Error('attach connection was not deferred');
    await heldAttach.received;
    cleanup.release();
    await w.waitForLog(`waiting for the newer selection of run ${launched.id}`);
    expect(w.link.helper.stops).toEqual([]);

    heldAttach.release();
    await expect(adopting).rejects.toThrow('the cleanup connection failed');
    expect(await older).toBe('cancelled');
    expect(w.link.helper.stops).toEqual([{instanceId: launched.id, force: false}]);
    expect(w.link.helper.host.instances.map(instance => instance.id)).toEqual([RUN_B]);
    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a superseded resume of an already-live run never stops that run', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.link.helper.deferNext('run.start');

    const older = w.runs.resume(HOST, '/p', exactResumeRunId('run-a'));
    await delayed.received;
    await w.runs.attach(HOST, RUN_B);
    delayed.release();
    await older;

    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.link.helper.stops).toEqual([]);
    expect(w.link.helper.host.instances.map(entry => entry.id)).toEqual([RUN_A, RUN_B]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a newer attach wins while the older selection waits to be remembered', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.recents.deferNext();

    const older = w.runs.attach(HOST, RUN_A);
    await delayed.received;
    const newer = w.runs.attach(HOST, RUN_B);
    delayed.release();
    await Promise.all([older, newer]);

    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.remembered.map(run => run.runId)).toEqual(['run-b']);
    expect(w.pages).toMatchObject({opened: 1, closed: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('detach invalidates a selection waiting to be remembered', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    const delayed = w.recents.deferNext();

    const attaching = w.runs.attach(HOST, RUN_A);
    await delayed.received;
    expect(w.connection.held).toBe(true);
    w.runs.detach();
    delayed.release();
    await attaching;

    expect(w.runs.attached()).toBeNull();
    expect(w.remembered).toEqual([]);
    expect(w.pages.opened).toBe(0);
    expect(w.connection.held).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a stale-run cleanup failure is logged without changing the newer binding', async () => {
    const w = world({failConnectionCall: 3});
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', []);
    await delayed.received;
    await w.runs.attach(HOST, RUN_B);
    delayed.release();
    expect(await older).toBe('cancelled');

    expect(w.calls.connections).toBe(3);
    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.logs).toContain(
      'could not stop superseded run 000000000001: the cleanup connection failed',
    );
    expect(w.pages).toMatchObject({opened: 1, closed: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('an attached run holds its connection open until it is detached', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000c', 'run-c', '/p'));
    await w.runs.attach({kind: 'local'}, '00000000000c');
    expect(w.connection.held).toBe(true);
    w.runs.detach();
    expect(w.connection.held).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });

  test('pages closed before host recovery do not open stale run channels afterward', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    let runConnections = 0;
    w.link.helper.host.runs.set(RUN_A, () => {
      runConnections += 1;
    });
    await w.runs.attach(HOST, RUN_A);
    const recovering = w.link.deferEstablish();
    w.link.helper.drop();
    await recovering.received;

    const ports = Array.from({length: 4}, () => new FakeRelayPort());
    for (const port of ports) {
      w.runs.connect(port, 'run-control');
      port.disconnect();
      port.disconnect();
    }
    expect(ports.every(port => port.closed)).toBe(true);
    expect(ports.map(port => port.posted)).toEqual(
      ports.map(() => [{type: 'close'}] as MainToPage[]),
    );

    recovering.release();
    await phaseReached(w.connection, 'ready');
    await Promise.resolve();

    expect(runConnections).toBe(0);
    expect(w.link.helper.running).toBe(1);
    expect(w.connection.phase.kind).toBe('ready');
    expect(w.runs.attached()?.connection.kind).toBe('connected');
    await w.connection[Symbol.asyncDispose]();
  });

  test('regression: an attach leases its checkout connection while recent persistence waits', async () => {
    const link = new FakeLink();
    link.helper.publish(record(RUN_A, 'run-a', '/p'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      config: DEFAULT_CONNECTION_CONFIG,
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    const firstLease = await manager.acquire(HOST, '/a');
    const first = firstLease.connection;
    const recents = new FakeRecentRuns();
    const delayed = recents.deferNext();
    const runs = new RunControl({
      connection: async () => firstLease,
      openPage: () => ({wake: () => {}, close: () => {}}),
      confirm: async () => true,
      remember: (run, current) => recents.remember(run, current),
      changed: () => {},
      log: () => {},
      now: () => 7,
    });

    const attaching = runs.attach(HOST, RUN_A);
    await delayed.received;
    const secondLease = await manager.acquire(HOST, '/b');
    expect(first.phase.kind).toBe('ready');

    delayed.release();
    await attaching;
    expect(runs.attached()).toMatchObject({instanceId: RUN_A, connection: {kind: 'connected'}});
    expect(first.held).toBe(true);
    runs.detach();
    expect(first.phase.kind).toBe('closed');
    await secondLease[Symbol.asyncDispose]();
  });
});
