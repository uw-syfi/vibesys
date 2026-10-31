import {describe, expect, test} from 'bun:test';
import {
  ConnectionManager,
  type InspectedCandidate,
  type RegistrySetup,
  registrySetup,
  runWatch,
} from '@vibesys/connection';
import {FakeLink, FakeRunGateway, olderHost} from '@vibesys/connection/testing';
import {type HostId, type HostListing, hostKey} from '@vibesys/hosts';
import {SettingsStore, withHostSettings} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {checkHostVersion, HostVersionGate} from './host-version.js';
import {Hosts} from './index.js';

const ALPHA: HostId = {kind: 'ssh', alias: 'alpha'};
const signal = () => new AbortController().signal;
const found = {
  state: 'found' as const,
  root: '/src/vibesys',
  version: '0.1.1',
  commit: null,
  dirty: null,
  compatible: true,
};

describe('checkHostVersion', () => {
  const answering = (inspected: () => Promise<unknown>) =>
    ({
      inspectCandidate: inspected,
      completePaths: async () => {
        throw new Error('unused');
      },
    }) as unknown as Parameters<typeof checkHostVersion>[0];

  test('a found checkout is current; a checkout too old is out of date', async () => {
    expect(
      await checkHostVersion(
        answering(async () => found),
        ALPHA,
        '/c',
        signal(),
      ),
    ).toBe('current');
    const tooOld = {state: 'too_old', version: '0.0.9', min: '0.1.0'};
    expect(
      await checkHostVersion(
        answering(async () => tooOld),
        ALPHA,
        '/c',
        signal(),
      ),
    ).toBe('out-of-date');
  });

  test('a host that does not know the command is out of date, from its typed answer alone', async () => {
    const setup = registrySetup({unary: olderHost.read});
    expect(await checkHostVersion(setup, ALPHA, '/c', signal())).toBe('out-of-date');
  });

  test('a failure that is no verdict rejects instead of marking the host out of date', async () => {
    const down = answering(async () => {
      throw new Error('gateway closed');
    });
    await expect(checkHostVersion(down, ALPHA, '/c', signal())).rejects.toThrow('gateway closed');
  });
});

describe('Hosts on an older v2 host', () => {
  test('a live run stream is checked once and shown as incompatible, not as its listing', async () => {
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    await using manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000},
      log: () => {},
    });
    const pushed: HostListing[] = [];
    let outOfDate: () => void = () => {};
    const heard = new Promise<void>(resolve => {
      outOfDate = resolve;
    });
    const logged: string[] = [];
    const gateway = new FakeRunGateway();
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      setup: registrySetup({unary: olderHost.read}),
      runs: runWatch(gateway, new FakeClock().schedule),
      log: message => logged.push(message),
      push: listing => {
        pushed.push(listing);
        if (listing.kind === 'unavailable' && listing.connection.kind === 'incompatible') {
          outOfDate();
        }
      },
      retire: () => {},
    });

    await hosts.configured();
    gateway.announce(ALPHA, []);
    await heard;

    const last = pushed.at(-1);
    expect(last).toMatchObject({
      kind: 'unavailable',
      host: hostKey(ALPHA),
      step: 'runs',
      connection: {kind: 'incompatible'},
    });
    expect(JSON.stringify([pushed, logged])).not.toMatch(/usage|invalid choice|does not provide/i);
  });
});

const HOST_KEY = hostKey(ALPHA);
const listed = (id: string): HostListing => ({
  kind: 'listed',
  host: HOST_KEY,
  live: [],
  ended: [],
  unreadable: [id],
  unverified: [],
});
const incompatible = (listing: HostListing): boolean =>
  listing.kind === 'unavailable' && listing.connection.kind === 'incompatible';

/** One held inspection: the test answers it, or fails it, whenever it chooses. */
class HeldInspection {
  readonly signal: AbortSignal;
  readonly #settle: {
    resolve: (value: InspectedCandidate) => void;
    reject: (error: Error) => void;
  };
  readonly answer: Promise<InspectedCandidate>;

  constructor(signal: AbortSignal) {
    this.signal = signal;
    let settle = {resolve: (_: InspectedCandidate) => {}, reject: (_: Error) => {}};
    this.answer = new Promise<InspectedCandidate>((resolve, reject) => {
      settle = {resolve, reject};
    });
    this.#settle = settle;
  }

  current(): void {
    this.#settle.resolve(found);
  }

  tooOld(): void {
    this.#settle.resolve({state: 'too_old', version: '0.0.9', min: '0.1.0'});
  }

  fail(): void {
    this.#settle.reject(new Error('gateway closed'));
  }
}

/** A registry whose inspections are held until the test answers them; each is announced as it starts. */
class HeldSetup implements RegistrySetup {
  readonly inspections: HeldInspection[] = [];
  #waiting: (() => void)[] = [];

  /** Resolves when the n-th inspection (1-based) has started. */
  async started(n: number): Promise<HeldInspection> {
    while (this.inspections.length < n) {
      await new Promise<void>(resolve => this.#waiting.push(resolve));
    }
    return this.inspections[n - 1] as HeldInspection;
  }

  inspectCandidate(
    _host: HostId,
    _path: string,
    _revision: bigint,
    signal: AbortSignal,
  ): Promise<InspectedCandidate> {
    const held = new HeldInspection(signal);
    this.inspections.push(held);
    for (const wake of this.#waiting.splice(0)) wake();
    return held.answer;
  }

  async completePaths(): Promise<never> {
    throw new Error('unused');
  }
}

/** The gate over a host whose checks are held, with everything it pushed and every check it made. */
function gateOver() {
  const pushed: HostListing[] = [];
  const logged: string[] = [];
  const checks: {
    signal: AbortSignal;
    answer: (outcome: 'current' | 'out-of-date' | Error) => void;
  }[] = [];
  const gate = new HostVersionGate({
    host: HOST_KEY,
    check: signal =>
      new Promise((resolve, reject) => {
        checks.push({
          signal,
          answer: outcome => (outcome instanceof Error ? reject(outcome) : resolve(outcome)),
        });
      }),
    push: listing => pushed.push(listing),
    log: reason => logged.push(reason),
  });
  const settled = async (): Promise<void> => {
    await Promise.resolve();
    await Promise.resolve();
  };
  return {gate, pushed, logged, checks, settled};
}

describe('HostVersionGate', () => {
  test('a host is shown before any check has answered', () => {
    const {gate, pushed, checks} = gateOver();
    gate.recheck();
    gate.listing(listed('a'));
    expect(pushed).toEqual([listed('a')]);
    expect(checks).toHaveLength(1);
  });

  test('an out-of-date verdict withholds listings and a later current verdict republishes the latest', async () => {
    const {gate, pushed, checks, settled} = gateOver();
    gate.recheck();
    gate.listing(listed('a'));
    checks[0]?.answer('out-of-date');
    await settled();
    gate.listing(listed('b'));
    gate.listing(listed('c'));
    expect(pushed.map(incompatible)).toEqual([false, true]);

    gate.recheck();
    checks[1]?.answer('current');
    await settled();
    expect(pushed.at(-1)).toEqual(listed('c'));
    expect(pushed).toHaveLength(3);
  });

  test('a retry runs the check again, so a host that was updated stops needing one', async () => {
    const {gate, pushed, checks, settled} = gateOver();
    gate.recheck();
    checks[0]?.answer('out-of-date');
    await settled();
    gate.listing(listed('a'));
    expect(checks).toHaveLength(1);

    gate.recheck();
    expect(checks).toHaveLength(2);
    checks[1]?.answer('current');
    await settled();
    expect(pushed.at(-1)).toEqual(listed('a'));
  });

  test('an answer from an older generation is never applied', async () => {
    const {gate, pushed, checks, settled} = gateOver();
    gate.recheck();
    gate.recheck();
    expect(checks[0]?.signal.aborted).toBe(true);
    expect(checks[1]?.signal.aborted).toBe(false);
    checks[1]?.answer('current');
    await settled();
    checks[0]?.answer('out-of-date');
    await settled();
    gate.listing(listed('a'));
    expect(pushed).toEqual([listed('a')]);
  });

  test('an answer that arrives after the connection left ready is dropped', async () => {
    const {gate, pushed, checks, settled} = gateOver();
    gate.recheck();
    gate.invalidate();
    expect(checks[0]?.signal.aborted).toBe(true);
    checks[0]?.answer('out-of-date');
    await settled();
    gate.listing(listed('a'));
    expect(pushed).toEqual([listed('a')]);
  });

  test('a check that fails is no verdict: it never hides a host, and it clears an earlier one', async () => {
    const {gate, pushed, logged, checks, settled} = gateOver();
    gate.recheck();
    gate.listing(listed('a'));
    checks[0]?.answer(new Error('gateway closed'));
    await settled();
    expect(pushed).toEqual([listed('a')]);
    expect(logged).toEqual(['gateway closed']);

    gate.recheck();
    checks[1]?.answer('out-of-date');
    await settled();
    gate.listing(listed('b'));
    gate.recheck();
    checks[2]?.answer(new Error('gateway closed'));
    await settled();
    expect(pushed.at(-1)).toEqual(listed('b'));
  });
});

describe('Hosts and a held version check', () => {
  async function hostsOver(setup: RegistrySetup) {
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000},
      log: () => {},
    });
    const gateway = new FakeRunGateway();
    const clock = new FakeClock();
    const pushed: HostListing[] = [];
    const wake: (() => void)[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      setup,
      runs: runWatch(gateway, clock.schedule),
      log: () => {},
      push: listing => {
        pushed.push(listing);
        for (const resolve of wake.splice(0)) resolve();
      },
      retire: () => {},
    });
    /** Resolves once `count` pushes, counted from the start, satisfy `enough`. */
    const until = async (enough: (all: readonly HostListing[]) => boolean): Promise<void> => {
      while (!enough(pushed)) await new Promise<void>(resolve => wake.push(resolve));
    };
    return {hosts, manager, gateway, clock, pushed, until};
  }

  test('Retry asks the checkout again, and a host that was updated is shown again', async () => {
    const setup = new HeldSetup();
    const {hosts, manager, gateway, until} = await hostsOver(setup);
    await using _manager = manager;
    await hosts.configured();
    gateway.announce(ALPHA, []);
    (await setup.started(1)).tooOld();
    await until(all => all.some(incompatible));

    const retrying = hosts.signIn(ALPHA);
    (await setup.started(2)).current();
    await retrying;
    await until(all => all.at(-1)?.kind === 'listed');
  });

  test("a check held across a reconnect never overrides the newer connection's verdict", async () => {
    const setup = new HeldSetup();
    const {hosts, manager, gateway, clock, pushed, until} = await hostsOver(setup);
    await using _manager = manager;
    await hosts.configured();
    gateway.announce(ALPHA, []);
    const first = await setup.started(1);

    for (const stream of gateway.open(ALPHA)) stream.fail(new Error('network loss'));
    await until(all => all.at(-1)?.kind === 'unavailable');
    clock.advanceBy(1_000);
    gateway.announce(ALPHA, []);
    const second = await setup.started(2);
    expect(first.signal.aborted).toBe(true);
    second.current();
    await until(all => all.at(-1)?.kind === 'listed');
    first.tooOld();
    // The stale answer has no effect; the retry below is the next observable event after it.
    const retrying = hosts.signIn(ALPHA);
    (await setup.started(3)).current();
    await retrying;

    expect(pushed.some(incompatible)).toBe(false);
  });
});
