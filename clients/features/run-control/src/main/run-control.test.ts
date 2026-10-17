import {describe, expect, test} from 'bun:test';
import {
  type ConnectionLease,
  ConnectionManager,
  DEFAULT_CONNECTION_CONFIG,
  HostConnection,
  type Phase,
} from '@vibesys/connection';
import {FakeLink, phaseReached, record} from '@vibesys/connection/testing';
import type {InstanceList} from '@vibesys/contracts/host-helper';
import type {ConnectionStatus} from '@vibesys/hosts';
import type {MainToPage} from '@vibesys/ipc';
import {exactResumeRunId, type RunRef} from '@vibesys/runs';
import type {StoredRecentRun} from '@vibesys/settings';
import {FakeClock} from '@vibesys/transport/testing';
import type {RelayPort} from './relay.js';
import {RunControl} from './run-control.js';

const HOST = {kind: 'local'} as const;
const RUN_A = '00000000000a';
const RUN_B = '00000000000b';

function runRef(runId: string): RunRef {
  return {host: 'local', runId};
}

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

type LeaseFailure = {readonly error: Error; readonly mode: 'reject' | 'throw'};

function lease(
  connection: HostConnection,
  onRelease: () => void = () => {},
  failure: LeaseFailure | null = null,
): ConnectionLease {
  const release = connection.hold();
  let done = false;
  return {
    connection,
    [Symbol.asyncDispose]() {
      if (done) return Promise.resolve();
      done = true;
      release();
      onRelease();
      if (failure?.mode === 'throw') throw failure.error;
      return failure === null ? Promise.resolve() : Promise.reject(failure.error);
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

type SubscriptionStage = 'onPhase' | 'watchInstances' | 'onRunUnreachable';
type BindingStage = 'openPage' | SubscriptionStage | 'changed';

/** A real FakeLink-backed connection with deterministic registration faults and ownership counts. */
class FaultInjectingHostConnection extends HostConnection {
  readonly subscriptions: Record<SubscriptionStage, number> = {
    onPhase: 0,
    watchInstances: 0,
    onRunUnreachable: 0,
  };
  readonly #fail: (stage: BindingStage) => void;

  constructor(
    options: ConstructorParameters<typeof HostConnection>[0],
    fail: (stage: BindingStage) => void,
  ) {
    super(options);
    this.#fail = fail;
  }

  override onPhase(listener: (phase: Phase) => void): () => void {
    this.#fail('onPhase');
    return this.#tracked('onPhase', super.onPhase(listener));
  }

  override watchInstances(listener: (listing: InstanceList) => void): () => void {
    this.#fail('watchInstances');
    return this.#tracked('watchInstances', super.watchInstances(listener));
  }

  override onRunUnreachable(listener: (instanceId: string, detail: string) => void): () => void {
    this.#fail('onRunUnreachable');
    return this.#tracked('onRunUnreachable', super.onRunUnreachable(listener));
  }

  #tracked(stage: SubscriptionStage, unsubscribe: () => void): () => void {
    this.subscriptions[stage] += 1;
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      this.subscriptions[stage] -= 1;
      unsubscribe();
    };
  }
}

/** A RunControl over one FakeHelper host, with the pages it opened and the runs it remembered. */
function world(
  options: {
    readonly failConnectionCall?: number;
    readonly deferConnectionCall?: number;
    readonly deferConnectionCalls?: readonly number[];
    readonly failBindingStage?: BindingStage;
    readonly failLeaseReleaseCall?: number;
    readonly leaseFailureMode?: LeaseFailure['mode'];
  } = {},
) {
  const link = new FakeLink();
  link.helper.host.projects.set('/p', ['spsc']);
  let failurePending = options.failBindingStage !== undefined;
  const fail = (stage: BindingStage): void => {
    if (!failurePending || options.failBindingStage !== stage) return;
    failurePending = false;
    throw new Error(`binding ${stage} failed`);
  };
  const connection = new FaultInjectingHostConnection(
    {
      link,
      checkout: '/src/vibesys',
      appVersion: 'test',
      config: DEFAULT_CONNECTION_CONFIG,
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    },
    fail,
  );
  const recents = new FakeRecentRuns();
  const pages = {opened: 0, closed: 0, woken: 0, confirms: 0};
  const log = new FakeLog();
  const calls = {connections: 0, releases: 0, changes: 0};
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
      return lease(
        connection,
        () => {
          calls.releases += 1;
        },
        call === options.failLeaseReleaseCall
          ? {
              error: new Error('retirement remained unconfirmed'),
              mode: options.leaseFailureMode ?? 'reject',
            }
          : null,
      );
    },
    openPage: () => {
      fail('openPage');
      pages.opened += 1;
      return {wake: () => (pages.woken += 1), close: () => (pages.closed += 1)};
    },
    confirm: async () => {
      pages.confirms += 1;
      return true;
    },
    remember: (run, current) => recents.remember(run, current),
    changed: () => {
      calls.changes += 1;
      fail('changed');
    },
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
  test('retiring a start owner stops a late launch without opening its page', async () => {
    const w = world();
    const delayed = w.link.helper.deferNext('run.start');
    const starting = w.runs.start(HOST, '/p', 'spsc', [], {
      owner: '10:100',
      sequence: 1,
    });
    await delayed.received;

    w.runs.retireStartOwner('10:100');
    delayed.release();

    expect(await starting).toBe('cancelled');
    expect(w.runs.attached()).toBeNull();
    expect(w.pages).toMatchObject({opened: 0, closed: 0});
    expect(w.link.helper.stops).toEqual([{instanceId: '000000000001', force: false}]);
    expect(w.link.helper.host.instances).toEqual([]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('retiring a pending start preserves the run that was already attached', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    await w.runs.attach(HOST, RUN_A);
    const delayed = w.link.helper.deferNext('run.start');
    const starting = w.runs.start(HOST, '/p', 'spsc', [], {
      owner: '10:100',
      sequence: 1,
    });
    await delayed.received;

    w.runs.retireStartOwner('10:100');
    expect(w.runs.attached()?.instanceId).toBe(RUN_A);
    delayed.release();

    expect(await starting).toBe('cancelled');
    expect(w.runs.attached()?.instanceId).toBe(RUN_A);
    expect(w.pages).toMatchObject({opened: 1, closed: 0});
    expect(w.link.helper.stops).toEqual([{instanceId: '000000000001', force: false}]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('equal start sequences from different owners are independent', async () => {
    const w = world();
    const olderReply = w.link.helper.deferNext('run.start');
    const older = w.runs.start(HOST, '/p', 'spsc', [], {owner: '10:100', sequence: 1});
    await olderReply.received;
    const newerReply = w.link.helper.deferNext('run.start');
    const newer = w.runs.start(HOST, '/p', 'spsc', [], {owner: '11:101', sequence: 1});
    await newerReply.received;

    w.runs.retireStartOwner('10:100');
    newerReply.release();
    expect(await newer).toBe('started');
    const selected = w.runs.attached()?.instanceId;
    olderReply.release();
    expect(await older).toBe('cancelled');

    expect(selected).toBe('000000000002');
    expect(w.runs.attached()?.instanceId).toBe(selected);
    expect(w.link.helper.stops).toEqual([{instanceId: '000000000001', force: false}]);
    expect(w.pages).toMatchObject({opened: 1, closed: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('a successor cancellation cannot cancel a predecessor with the same sequence', async () => {
    const w = world();
    const delayed = w.link.helper.deferNext('run.start');
    const starting = w.runs.start(HOST, '/p', 'spsc', [], {
      owner: '10:100',
      sequence: 1,
    });
    await delayed.received;

    w.runs.cancelStart({owner: '11:101', sequence: 1});
    delayed.release();

    expect(await starting).toBe('started');
    expect(w.pages).toMatchObject({opened: 1, closed: 0});
    expect(w.link.helper.stops).toEqual([]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('duplicate start identities within one owner remain rejected', async () => {
    const w = world();
    const delayed = w.link.helper.deferNext('run.start');
    const identity = {owner: '10:100', sequence: 1};
    const first = w.runs.start(HOST, '/p', 'spsc', [], identity);
    await delayed.received;

    await expect(w.runs.start(HOST, '/p', 'spsc', [], identity)).rejects.toThrow(
      'that start was already asked for',
    );

    w.runs.cancelStart(identity);
    delayed.release();
    expect(await first).toBe('cancelled');
    await w.connection[Symbol.asyncDispose]();
  });

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

  test('a chosen harness reaches run.start as typed data; no choice sends no harness field', async () => {
    const w = world();
    const harness = {provider: 'claude', model: 'claude-a'};
    expect(await w.runs.start(HOST, '/p', 'spsc', [], null, harness)).toBe('started');
    expect(await w.runs.start(HOST, '/p', 'spsc', ['--max-rounds', '2'])).toBe('started');
    const starts = w.link.helper.requests.filter(request => request.type === 'run.start');
    expect(starts.map(request => ('harness' in request ? request.harness : 'absent'))).toEqual([
      harness,
      'absent',
    ]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a harness the host does not offer fails the start without launching', async () => {
    const w = world();
    const harness = {provider: 'claude', model: 'not-vetted'};
    await expect(w.runs.start(HOST, '/p', 'spsc', [], null, harness)).rejects.toThrow(
      'vetted models: claude-a',
    );
    expect(w.link.helper.host.instances).toEqual([]);
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

  test('an older attach that answers late still binds independently alongside a newer attach to a different target', async () => {
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

    // The older attach's target differs from the newer one's, so its late answer still binds it;
    // `attached()` (the title strip's single legacy value) now reflects whichever bound last.
    expect(w.runs.attached()?.instanceId).toBe(RUN_A);
    expect(w.pages).toMatchObject({opened: 2, closed: 0});
    expect(w.remembered.map(run => run.runId)).toEqual(['run-b', 'run-a']);
    expect(w.link.helper.stops).toEqual([]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('two attaches to different targets both bind, independently, each remembered', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));

    await w.runs.attach(HOST, RUN_A);
    await w.runs.attach(HOST, RUN_B);

    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.pages).toMatchObject({opened: 2, closed: 0});
    expect(w.remembered.map(run => run.runId)).toEqual(['run-a', 'run-b']);
    await w.connection[Symbol.asyncDispose]();
  });

  test('same run id on different hosts keeps both bindings and routes independently', async () => {
    const localLink = new FakeLink();
    const remoteLink = new FakeLink();
    localLink.helper.publish(record(RUN_A, 'shared-run', '/local'));
    remoteLink.helper.publish(record(RUN_B, 'shared-run', '/remote'));
    const makeConnection = (link: FakeLink): HostConnection =>
      new HostConnection({
        link,
        checkout: '/src/vibesys',
        appVersion: 'test',
        config: DEFAULT_CONNECTION_CONFIG,
        scheduleTimeout: new FakeClock().scheduleTimeout,
        log: () => {},
      });
    const local = makeConnection(localLink);
    const remote = makeConnection(remoteLink);
    const localRun = {host: 'local', runId: 'shared-run'} as const;
    const remoteRun = {host: 'ssh:bravo', runId: 'shared-run'} as const;
    const pages = new Map<string, {closed: number}>();
    const releases = {local: 0, remote: 0};
    const runs = new RunControl({
      connection: async id =>
        id.kind === 'local'
          ? lease(local, () => (releases.local += 1))
          : lease(remote, () => (releases.remote += 1)),
      openPage: run => {
        const page = {closed: 0};
        pages.set(run.host, page);
        return {wake: () => {}, close: () => (page.closed += 1)};
      },
      confirm: async () => true,
      remember: async () => {},
      changed: () => {},
      log: () => {},
      now: () => 7,
    });

    await runs.attach(HOST, RUN_A);
    await runs.attach({kind: 'ssh', alias: 'bravo'}, RUN_B);

    expect([...pages.values()].map(page => page.closed)).toEqual([0, 0]);
    expect(releases).toEqual({local: 0, remote: 0});
    expect(await runs.logs(localRun, {kind: 'list'})).toMatchObject({
      ok: true,
      instanceId: RUN_A,
    });
    expect(await runs.logs(remoteRun, {kind: 'list'})).toMatchObject({
      ok: true,
      instanceId: RUN_B,
    });

    runs.detach(localRun);
    expect(pages.get('local')?.closed).toBe(1);
    expect(pages.get('ssh:bravo')?.closed).toBe(0);
    expect(releases).toEqual({local: 1, remote: 0});
    expect(await runs.logs(remoteRun, {kind: 'list'})).toMatchObject({
      ok: true,
      instanceId: RUN_B,
    });

    runs.detach(remoteRun);
    await local[Symbol.asyncDispose]();
    await remote[Symbol.asyncDispose]();
  });

  test('an older attach proceeds unaffected when an attach to a different target fails', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    const delayed = w.link.helper.deferNext('instances.list');

    const older = w.runs.attach(HOST, RUN_A);
    await delayed.received;
    await expect(w.runs.attach(HOST, RUN_B)).rejects.toThrow(`no live run ${RUN_B}`);
    delayed.release();
    await older;

    expect(w.runs.attached()?.instanceId).toBe(RUN_A);
    expect(w.pages.opened).toBe(1);
    expect(w.remembered.map(run => run.runId)).toEqual(['run-a']);
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    [
      'start',
      async (runs: RunControl) => {
        expect(await runs.start(HOST, '/p', 'spsc', [])).toBe('started');
      },
    ],
    [
      'resume',
      async (runs: RunControl) => {
        await runs.resume(HOST, '/p', exactResumeRunId('run-a'));
      },
    ],
  ] as const)('a %s to a new instance completes independently alongside an attach to a different, already-live target', async (_kind, launch) => {
    const w = world();
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.link.helper.deferNext('run.start');

    const older = launch(w.runs);
    await delayed.received;
    await w.runs.attach(HOST, RUN_B);
    delayed.release();
    await older;

    // Neither target touches the other's claim: the attach binds immediately, and the launch's
    // own binding lands once its deferred reply arrives, becoming the last one bound.
    expect(w.runs.attached()?.instanceId).toBe('000000000001');
    expect(w.pages).toMatchObject({opened: 2, closed: 0, confirms: 0});
    expect(w.link.helper.stops).toEqual([]);
    expect(w.link.helper.host.instances.map(entry => entry.id)).toEqual([RUN_B, '000000000001']);
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
    const w = world({deferConnectionCall: 2});
    const delayedStart = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', [], {owner: 'doc', sequence: 1});
    await delayedStart.received;
    const launched = w.link.helper.host.instances[0];
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    // Withdraw the start explicitly: a different-target attach no longer supersedes it, so this is
    // the one way, besides a same-target race, to abandon a launch before it ever claims a target.
    w.runs.cancelStart({owner: 'doc', sequence: 1});
    delayedStart.release();
    const cleanup = w.deferredConnection;
    if (cleanup === null) throw new Error('cleanup connection was not deferred');
    await cleanup.received;

    const adopting = w.runs.attach(HOST, launched.id);
    cleanup.release();
    await w.waitForLog(`waiting for the newer selection of run ${launched.id}`);
    expect(w.link.helper.stops).toEqual([]);

    const [, outcome] = await Promise.all([adopting, older]);
    expect(outcome).toBe('cancelled');
    expect(w.runs.attached()?.instanceId).toBe(launched.id);
    expect(w.link.helper.host.instances.map(instance => instance.id)).toEqual([launched.id]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a withdrawn stale cleanup stops once when its new same-target attach fails', async () => {
    const w = world({deferConnectionCalls: [2, 3], failConnectionCall: 3});
    const delayedStart = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', [], {owner: 'doc', sequence: 1});
    await delayedStart.received;
    const launched = w.link.helper.host.instances[0];
    if (launched === undefined) throw new Error('run.start did not publish its serving record');
    w.runs.cancelStart({owner: 'doc', sequence: 1});
    delayedStart.release();
    const cleanup = w.deferredConnectionAt(2);
    if (cleanup === null) throw new Error('cleanup connection was not deferred');
    await cleanup.received;

    const adopting = w.runs.attach(HOST, launched.id);
    const heldAttach = w.deferredConnectionAt(3);
    if (heldAttach === null) throw new Error('attach connection was not deferred');
    await heldAttach.received;
    cleanup.release();
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

  test('a resume of an already-live run binds independently alongside an attach to a different target, without stopping it', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.publish(record(RUN_B, 'run-b', '/p'));
    const delayed = w.link.helper.deferNext('run.start');

    const older = w.runs.resume(HOST, '/p', exactResumeRunId('run-a'));
    await delayed.received;
    await w.runs.attach(HOST, RUN_B);
    delayed.release();
    await older;

    // The resume discovers run-a is already live and adopts that same record (`alreadyLive`),
    // which binds after the attach to the unrelated RUN_B and so becomes the last one bound.
    expect(w.runs.attached()?.instanceId).toBe(RUN_A);
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

    // Different targets: both bind, and both get remembered once the gated commit lets the first
    // (queued ahead of the second in the one recent-runs writer) go through.
    expect(w.runs.attached()?.instanceId).toBe(RUN_B);
    expect(w.remembered.map(run => run.runId)).toEqual(['run-a', 'run-b']);
    expect(w.pages).toMatchObject({opened: 2, closed: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('detach of a still-attaching target is a no-op: no window exists yet to close', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    const delayed = w.recents.deferNext();

    const attaching = w.runs.attach(HOST, RUN_A);
    await delayed.received;
    expect(w.connection.held).toBe(true);
    // The eventual run id is already known (the published record's), but nothing is bound to it
    // yet, so no page has opened for it to close. Detaching it here must not reach into the claim
    // still navigating toward it; the attach proceeds exactly as if this call had not happened.
    w.runs.detach(runRef('run-a'));
    delayed.release();
    await attaching;

    expect(w.runs.attached()).toMatchObject({instanceId: RUN_A, runId: 'run-a'});
    expect(w.remembered.map(run => run.runId)).toEqual(['run-a']);
    expect(w.pages.opened).toBe(1);
    expect(w.connection.held).toBe(true);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a stale-run cleanup failure is logged and leaves nothing bound', async () => {
    const w = world({failConnectionCall: 2});
    const delayed = w.link.helper.deferNext('run.start');

    const older = w.runs.start(HOST, '/p', 'spsc', [], {owner: 'doc', sequence: 1});
    await delayed.received;
    w.runs.cancelStart({owner: 'doc', sequence: 1});
    delayed.release();
    expect(await older).toBe('cancelled');

    expect(w.calls.connections).toBe(2);
    expect(w.runs.attached()).toBeNull();
    await w.waitForLog('could not stop superseded run');
    expect(w.logs).toContain(
      'could not stop superseded run 000000000001: the cleanup connection failed',
    );
    expect(w.pages).toMatchObject({opened: 0, closed: 0});
    await w.connection[Symbol.asyncDispose]();
  });

  test('an attached run holds its connection open until it is detached', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000c', 'run-c', '/p'));
    await w.runs.attach({kind: 'local'}, '00000000000c');
    expect(w.connection.held).toBe(true);
    w.runs.detach(runRef('run-c'));
    expect(w.connection.held).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    'reject',
    'throw',
  ] as const)('detach observes a lease retirement that %ss without delaying the UI transition', async mode => {
    const w = world({failLeaseReleaseCall: 1, leaseFailureMode: mode});
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    await w.runs.attach(HOST, RUN_A);
    const message =
      'could not retire run 00000000000a on This Mac: retirement remained unconfirmed';

    expect(() => w.runs.detach(runRef('run-a'))).not.toThrow();
    expect(w.runs.attached()).toBeNull();
    expect(w.pages).toMatchObject({opened: 1, closed: 1});
    expect(w.calls.releases).toBe(1);
    await w.waitForLog(message);
    expect(w.logs).toEqual([message]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a failed old-binding retirement is observed once and leaves its replacement selected', async () => {
    const w = world({failLeaseReleaseCall: 1});
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    await w.runs.attach(HOST, RUN_A);
    const message =
      'could not retire run 00000000000a on This Mac: retirement remained unconfirmed';

    // Re-attaching to the same target replaces this run's own prior binding (`#releaseBinding`
    // for that same `run_id`), the one case where a second attach's bind does retire an older one.
    await w.runs.attach(HOST, RUN_A);
    expect(w.runs.attached()?.instanceId).toBe(RUN_A);
    expect(w.pages).toMatchObject({opened: 2, closed: 1});
    expect(w.calls.releases).toBe(1);
    await w.waitForLog(message);
    expect(w.logs).toEqual([message]);

    w.runs.detach(runRef('run-a'));
    expect(w.calls.releases).toBe(2);
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    ['openPage', 0],
    ['onPhase', 1],
    ['watchInstances', 1],
    ['onRunUnreachable', 1],
    ['changed', 1],
  ] as const)('a failed %s binding stage rolls every provisional resource back', async (stage, opened) => {
    const w = world({failBindingStage: stage});
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));

    await expect(w.runs.attach(HOST, RUN_A)).rejects.toThrow(`binding ${stage} failed`);

    expect(w.runs.attached()).toBeNull();
    expect(w.pages).toMatchObject({opened, closed: opened});
    expect(w.connection.subscriptions).toEqual({
      onPhase: 0,
      watchInstances: 0,
      onRunUnreachable: 0,
    });
    expect(w.calls.releases).toBe(1);
    expect(w.connection.held).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });

  test.each([
    ['start', (runs: RunControl) => runs.start(HOST, '/p', 'spsc', [])],
    ['resume', (runs: RunControl) => runs.resume(HOST, '/p', exactResumeRunId('run-a'))],
  ] as const)('a failed %s publication releases its binding intent and resources', async (kind, run) => {
    const w = world({failBindingStage: 'changed'});
    if (kind === 'resume') w.link.helper.publish(record(RUN_A, 'run-a', '/p'));

    await expect(run(w.runs)).rejects.toThrow('binding changed failed');

    expect(w.runs.attached()).toBeNull();
    expect(w.pages).toMatchObject({opened: 1, closed: 1});
    expect(w.connection.subscriptions).toEqual({
      onPhase: 0,
      watchInstances: 0,
      onRunUnreachable: 0,
    });
    expect(w.calls.releases).toBe(1);
    expect(w.connection.held).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a failed same-target binding settles the stale launch waiting on adoption', async () => {
    const w = world({failBindingStage: 'changed'});
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
    delayedAttach.release();
    await expect(adopting).rejects.toThrow('binding changed failed');
    expect(await older).toBe('cancelled');

    expect(w.runs.attached()).toBeNull();
    expect(w.pages).toMatchObject({opened: 1, closed: 1});
    expect(w.link.helper.stops).toEqual([{instanceId: launched.id, force: false}]);
    expect(w.link.helper.host.instances).toEqual([]);
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
      w.runs.connect(port, runRef('run-a'));
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
    runs.detach(runRef('run-a'));
    expect(first.phase.kind).toBe('closed');
    await secondLease[Symbol.asyncDispose]();
  });
});

describe('RunControl logs', () => {
  test("serves a page's logs from its binding's instance over one logs channel", async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    const store = w.link.helper.logStore;
    store.open(RUN_A, '000001', {kind: 'startup_step', stage: 'building_image'}, 'docker build');
    store.write(RUN_A, '000001', new TextEncoder().encode('building image 7/8: WORKDIR\n'));
    await w.runs.attach(HOST, RUN_A);
    const spawned = w.link.helper.spawned;

    const listed = await w.runs.logs(runRef('run-a'), {kind: 'list'});
    expect(listed).toMatchObject({ok: true, instanceId: RUN_A, streams: [{id: '000001'}]});
    const read = await w.runs.logs(runRef('run-a'), {
      kind: 'read',
      token: 'r1',
      read: {
        instance_id: RUN_A,
        stream_id: '000001',
        start: {tail_bytes: 4096},
        max_bytes: 4096,
        wait_ms: 0,
      },
    });
    expect(read).toMatchObject({ok: true, chunk: {data: 'building image 7/8: WORKDIR\n'}});
    expect(w.link.helper.spawned).toBe(spawned + 1);

    const logsChannel = w.link.helper.channels.at(-1);
    w.runs.detach(runRef('run-a'));
    await logsChannel?.process.exit;
    expect(logsChannel?.process.running).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });

  test('refuses a read that names another instance, and a page with no run', async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    await w.runs.attach(HOST, RUN_A);
    const read = {
      instance_id: RUN_B,
      stream_id: '000001',
      start: {offset: 0},
      max_bytes: 4096,
      wait_ms: 0,
    };

    expect(await w.runs.logs(runRef('run-a'), {kind: 'read', token: 'r1', read})).toMatchObject({
      ok: false,
      failure: {code: 'unknown_instance'},
    });
    expect(await w.runs.logs(runRef('run-z'), {kind: 'list'})).toMatchObject({
      ok: false,
      failure: {code: 'unknown_instance'},
    });
    await w.connection[Symbol.asyncDispose]();
  });

  test("a withdraw aborts the page's read with that token, and withdraws its long poll", async () => {
    const w = world();
    w.link.helper.publish(record(RUN_A, 'run-a', '/p'));
    w.link.helper.logStore.open(
      RUN_A,
      '000001',
      {kind: 'startup_step', stage: 'building_image'},
      'build',
    );
    await w.runs.attach(HOST, RUN_A);
    const read = {
      instance_id: RUN_A,
      stream_id: '000001',
      start: {offset: 0},
      max_bytes: 4096,
      wait_ms: 25_000,
    };
    const heldFirst = w.link.helper.deferNext('logs.read');
    const heldSecond = w.link.helper.deferNext('logs.read');
    const first = w.runs.logs(runRef('run-a'), {kind: 'read', token: 'r1', read});
    const second = w.runs.logs(runRef('run-a'), {kind: 'read', token: 'r2', read});
    await heldFirst.received;
    await heldSecond.received;

    expect(await w.runs.logs(runRef('run-a'), {kind: 'withdraw', token: 'unknown'})).toBeNull();
    expect(await w.runs.logs(runRef('run-a'), {kind: 'withdraw', token: 'r1'})).toBeNull();
    expect(await first).toMatchObject({ok: false, failure: {code: 'transport'}});
    heldSecond.release();
    expect(await second).toMatchObject({ok: true});
    expect(await w.runs.logs(runRef('run-a'), {kind: 'withdraw', token: 'r2'})).toBeNull();
    expect(await w.runs.logs(runRef('run-z'), {kind: 'withdraw', token: 'r1'})).toBeNull();

    // The logs channel keeps order: a withdrawal written before this listing reached the helper.
    expect(await w.runs.logs(runRef('run-a'), {kind: 'list'})).toMatchObject({ok: true});
    const [firstRead] = w.link.helper.requests.filter(request => request.type === 'logs.read');
    const cancels = w.link.helper.requests.filter(request => request.type === 'logs.cancel');
    expect(cancels).toMatchObject([{read_id: firstRead?.request_id}]);
    heldFirst.release();
    w.runs.detach(runRef('run-a'));
    await w.connection[Symbol.asyncDispose]();
  });
});
