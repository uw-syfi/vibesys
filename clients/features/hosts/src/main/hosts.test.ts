import {describe, expect, test} from 'bun:test';
import {join} from 'node:path';
import {
  type ConnectionLease,
  ConnectionManager,
  type RegistryRunReads,
  registrySetup,
  runWatch,
} from '@vibesys/connection';
import {
  FakeLink,
  FakeReadCatalog,
  FakeRunGateway,
  idleRunWatch,
  olderHost,
  record,
} from '@vibesys/connection/testing';
import {RUN_EVENT_TYPES_BY_KINDS} from '@vibesys/contracts/host-helper';
import {type HostId, type HostListing, hostKey, usageWindowRefusal} from '@vibesys/hosts';
import {FailureCategory, OperationFailed, RunLiveness} from '@vibesys/proto';
import {
  decodeRetainedSettings,
  encodeSettings,
  remember,
  SettingsStore,
  settingsBinaryPath,
  settingsFor,
  withHostSettings,
} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {Hosts, type SshConfigFilesystem} from './index.js';

const SSH_CONFIG = '/home/test/.ssh/config';
const LOCAL = {kind: 'local'} as const;
const ALPHA = {kind: 'ssh', alias: 'alpha'} as const;
const BETA = {kind: 'ssh', alias: 'beta'} as const;
const CONFIG = {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000};

/** A real manager that withholds one acquired lease after its shown connection already changed. */
class DeferredAcquireManager extends ConnectionManager {
  readonly releasedCheckouts: string[] = [];
  #gate: AcquireGate | null = null;

  override async acquire(host: HostId, checkout: string): Promise<ConnectionLease> {
    await this.#wait(checkout, 'before');
    const lease = await super.acquire(host, checkout);
    await this.#wait(checkout, 'after');
    let released = false;
    return {
      connection: lease.connection,
      [Symbol.asyncDispose]: async () => {
        if (released) return;
        released = true;
        this.releasedCheckouts.push(checkout);
        await lease[Symbol.asyncDispose]();
      },
    };
  }

  deferNext(
    checkout: string,
    position: AcquireGate['position'] = 'after',
  ): {readonly received: Promise<void>; readonly release: () => void} {
    if (this.#gate !== null) throw new Error('an acquisition is already deferred');
    let arrived = (): void => {};
    let release = (): void => {};
    const received = new Promise<void>(resolve => {
      arrived = resolve;
    });
    const released = new Promise<void>(resolve => {
      release = resolve;
    });
    this.#gate = {checkout, position, arrived, released};
    return {received, release};
  }

  async #wait(checkout: string, position: AcquireGate['position']): Promise<void> {
    const gate = this.#gate;
    if (gate?.checkout !== checkout || gate.position !== position) return;
    this.#gate = null;
    gate.arrived();
    await gate.released;
  }
}

interface AcquireGate {
  readonly checkout: string;
  readonly position: 'before' | 'after';
  readonly arrived: () => void;
  readonly released: Promise<void>;
}

/** An explicit SSH-config read result, with every requested path recorded. */
class FakeSshConfigFilesystem implements SshConfigFilesystem {
  readonly reads: string[] = [];

  constructor(readonly result: {readonly text: string} | {readonly error: unknown}) {}

  async readText(path: string, _maximumBytes: number): Promise<string> {
    this.reads.push(path);
    if ('error' in this.result) throw this.result.error;
    return this.result.text;
  }

  async realPath(path: string): Promise<string> {
    return path;
  }

  async *directoryEntries(_path: string): AsyncIterable<string> {
    // This focused Fake has no included files.
  }
}

function fileError(code: string): Error {
  return Object.assign(new Error(`${code}: ${SSH_CONFIG}`), {code});
}

/** Hold exactly one numbered settings read so another intent can deterministically pass it. */
class DeferredReadFilesystem extends FakeSettingsFilesystem {
  readonly received: Promise<void>;
  readonly #received: () => void;
  readonly #released: Promise<void>;
  readonly #release: () => void;
  readonly #heldRead: number;
  #reads = 0;

  constructor(files: Readonly<Record<string, string>>, heldRead = 1) {
    super(files);
    for (const [path, text] of Object.entries(files)) {
      this.setBytes(
        settingsBinaryPath(path),
        encodeSettings(decodeRetainedSettings(JSON.parse(text))),
      );
    }
    this.#heldRead = heldRead;
    let received = (): void => {};
    let release = (): void => {};
    this.received = new Promise(resolve => {
      received = resolve;
    });
    this.#released = new Promise(resolve => {
      release = resolve;
    });
    this.#received = received;
    this.#release = release;
  }

  override async readBytes(path: string): Promise<Uint8Array> {
    this.#reads += 1;
    if (this.#reads === this.#heldRead) {
      this.#received();
      await this.#released;
    }
    return super.readBytes(path);
  }

  release(): void {
    this.#release();
  }
}

/** Hold one read after its bytes were captured, while later reads and writes see newer bytes. */
class CapturedReadFilesystem extends FakeSettingsFilesystem {
  #gate: CapturedReadGate | null = null;

  override async readBytes(path: string): Promise<Uint8Array> {
    const bytes = await super.readBytes(path);
    const gate = this.#gate;
    if (gate !== null) {
      this.#gate = null;
      gate.captured();
      await gate.released;
    }
    return bytes;
  }

  deferNextRead(): {readonly captured: Promise<void>; readonly release: () => void} {
    if (this.#gate !== null) throw new Error('a settings read is already deferred');
    let captured = (): void => {};
    let release = (): void => {};
    const reached = new Promise<void>(resolve => {
      captured = resolve;
    });
    const released = new Promise<void>(resolve => {
      release = resolve;
    });
    this.#gate = {captured, released};
    return {captured: reached, release};
  }
}

interface CapturedReadGate {
  readonly captured: () => void;
  readonly released: Promise<void>;
}

async function configuredSettings(
  settings: SettingsStore,
  hosts: readonly HostId[],
): Promise<void> {
  for (const host of hosts) {
    await settings.updateHosts(current =>
      withHostSettings(current, host, {
        checkout: `/checkouts/${host.kind === 'local' ? 'local' : host.alias}`,
      }),
    );
  }
}

/** The public Hosts feature over a Fake SSH-config filesystem; no connection is opened. */
function subject(filesystem: SshConfigFilesystem): Hosts {
  const manager = new ConnectionManager({
    linkFor: () => {
      throw new Error('aliases must not open a host connection');
    },
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
  const settings = new SettingsStore({
    settingsPath: '/unused/hosts.json',
    recentPath: '/unused/recent.json',
    preferencesPath: '/unused/preferences.json',
    log: () => {},
  });
  return hostsInWorld({
    manager,
    settings,
    localCheckout: '/vibesys',
    sshConfigPath: SSH_CONFIG,
    sshConfigFilesystem: filesystem,
    log: () => {},
    push: () => {},
    retire: () => {},
  });
}

/** The public completion controller over one in-memory helper and settings store. */
function completionSubject(
  link: FakeLink,
  native?: {
    list: (
      host: HostId,
      project: string,
      signal: AbortSignal,
    ) => Promise<readonly import('@vibesys/hosts').ListedRun[]>;
  },
): {
  readonly hosts: Hosts;
  readonly manager: ConnectionManager;
} {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  const manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    config: CONFIG,
    log: () => {},
  });
  return {
    manager,
    hosts: hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      ...(native ? {savedRuns: {list: native.list}} : {}),
      log: () => {},
      push: () => {},
      retire: () => {},
    }),
  };
}

/** Native setup facts shared by checkout-race scenarios; helper leases remain separate. */
function hostsInWorld(
  deps: Omit<ConstructorParameters<typeof Hosts>[0], 'runs'> &
    Partial<Pick<ConstructorParameters<typeof Hosts>[0], 'runs'>>,
  peer = new FakeReadCatalog(),
): Hosts {
  for (const path of [
    '/new',
    '/newer',
    '/cancelled',
    '/new-beta',
    '/first',
    '/second',
    '/checkouts/alpha-new',
  ]) {
    peer.candidates.set(path, {root: '/src/vibesys', version: '0.1.1', compatible: true});
  }
  return new Hosts({
    setup: registrySetup(peer),
    runs: runWatch(new FakeRunGateway(), new FakeClock().schedule),
    ...deps,
  });
}

interface WorldOptions {
  /** Machines with a saved checkout. */
  readonly configured?: readonly HostId[];
  /** Whether a host's helper can start; the gateway is the same either way. */
  readonly helper?: 'up' | 'down';
  readonly filesystem?: FakeSettingsFilesystem;
  /** A host whose run watch cannot be opened. */
  readonly refuse?: string;
  /** A host whose first stop reports a failure after releasing its stream. */
  readonly failStopOnce?: string;
  /** Diagnostics and retirement callbacks that themselves fail. */
  readonly failingCallbacks?: boolean;
}

/**
 * The public Hosts over a Fake gateway: a host is watched exactly while its gateway has an open run
 * stream, and the helper is a link that counts how often anything asks for it.
 */
async function gatewayWorld(options: WorldOptions) {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: options.filesystem ?? new FakeSettingsFilesystem(),
    log: () => {},
  });
  await configuredSettings(settings, options.configured ?? []);
  const gateway = new FakeRunGateway();
  const clock = new FakeClock();
  const watch = runWatch(gateway, clock.schedule);
  let stopFailed = false;
  const runs = {
    observe: (host: HostId, listener: Parameters<typeof watch.observe>[1]) => {
      if (hostKey(host) === options.refuse) throw new Error(`cannot watch ${hostKey(host)}`);
      return watch.observe(host, listener);
    },
    cancel: (host: HostId) => {
      watch.cancel(host);
      if (hostKey(host) === options.failStopOnce && !stopFailed) {
        stopFailed = true;
        throw new Error('stop failed');
      }
    },
  };
  let acquisitions = 0;
  const manager = new ConnectionManager({
    linkFor: () => {
      acquisitions += 1;
      if (options.helper === 'down') throw new Error('helper unavailable; gateway still available');
      return new FakeLink();
    },
    appVersion: 'test',
    scheduleTimeout: clock.scheduleTimeout,
    config: CONFIG,
    log: () => {},
  });
  const peer = new FakeReadCatalog();
  const pushed: HostListing[] = [];
  const retired: string[] = [];
  const logged: string[] = [];
  const hosts = hostsInWorld(
    {
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: message => {
        logged.push(message);
        if (options.failingCallbacks) throw new Error('log failed');
      },
      push: listing => pushed.push(listing),
      retire: key => {
        retired.push(key);
        if (options.failingCallbacks) throw new Error('retire failed');
      },
      runs,
    },
    peer,
  );
  const everyHost: readonly HostId[] = [LOCAL, ALPHA, BETA];
  const settled = async (): Promise<void> => {
    for (let turn = 0; turn < 40; turn += 1) await Promise.resolve();
  };
  return {
    hosts,
    settings,
    gateway,
    clock,
    pushed,
    retired,
    logged,
    settled,
    helperAcquisitions: () => acquisitions,
    openStreams: (key: string) =>
      gateway.streams.filter(s => s.host === key && !s.signal.aborted).length,
    watched: () =>
      everyHost
        .filter(host => gateway.open(host).length > 0)
        .map(hostKey)
        .sort(),
    /** The checkouts the host-version check has asked the registry about, in order. */
    checked: () =>
      peer.requests.flatMap(request =>
        request.method.case === 'inspectCandidate' ? [request.method.value.path] : [],
      ),
    failStreams: async (host: HostId) => {
      for (const stream of gateway.open(host)) stream.fail(new Error('network loss'));
      await settled();
    },
  };
}

describe('Hosts.aliases', () => {
  test('reads and parses the configured SSH file', async () => {
    const filesystem = new FakeSshConfigFilesystem({text: 'Host gpu-box gpu-* jump\n'});

    expect(await subject(filesystem).aliases()).toEqual(['gpu-box', 'jump']);
    expect(filesystem.reads).toEqual([SSH_CONFIG]);
  });

  test('treats only a missing SSH config as an empty alias list', async () => {
    const filesystem = new FakeSshConfigFilesystem({error: fileError('ENOENT')});

    expect(await subject(filesystem).aliases()).toEqual([]);
    expect(filesystem.reads).toEqual([SSH_CONFIG]);
  });

  test('surfaces every other SSH config read failure unchanged', async () => {
    for (const code of ['EACCES', 'EISDIR', 'EIO']) {
      const error = fileError(code);
      const filesystem = new FakeSshConfigFilesystem({error});

      try {
        await subject(filesystem).aliases();
        throw new Error(`${code} unexpectedly resolved`);
      } catch (caught) {
        expect(caught).toBe(error);
      }
      expect(filesystem.reads).toEqual([SSH_CONFIG]);
    }
  });
});

describe('Hosts.complete', () => {
  test('uses the registry when transport v2 is on and does not open the helper', async () => {
    const manager = new ConnectionManager({
      linkFor: () => {
        throw new Error('the helper must stay closed');
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings: new SettingsStore({
        settingsPath: '/settings/hosts.json',
        recentPath: '/settings/recent.json',
        preferencesPath: '/settings/preferences.json',
        filesystem: new FakeSettingsFilesystem(),
        log: () => {},
      }),
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
      setup: {
        inspectCandidate: async () => {
          throw new Error('unused');
        },
        completePaths: async () => ({
          directory: '/manual',
          truncated: false,
          entries: [{name: 'project', path: '/manual/project', checkout: true, project: false}],
        }),
      },
    });
    await using _manager = manager;
    await expect(hosts.complete(LOCAL, '/manual/')).resolves.toEqual({
      kind: 'listed',
      directory: '/manual',
      entries: [{name: 'project', path: '/manual/project', checkout: true, project: false}],
      truncated: false,
    });
  });

  test('refuses setup when transport v2 is on and the registry setup is missing', async () => {
    const manager = new ConnectionManager({
      linkFor: () => {
        throw new Error('the helper must stay closed');
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    const hosts = new Hosts({
      runs: idleRunWatch(),
      manager,
      settings: new SettingsStore({
        settingsPath: '/settings/hosts.json',
        recentPath: '/settings/recent.json',
        preferencesPath: '/settings/preferences.json',
        filesystem: new FakeSettingsFilesystem(),
        log: () => {},
      }),
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await using _manager = manager;
    await expect(hosts.complete(LOCAL, '/manual/')).rejects.toThrow(
      'the registry setup is missing',
    );
  });
});

describe('Hosts.setCheckout transport v2', () => {
  function v2Hosts(setup: NonNullable<ConstructorParameters<typeof Hosts>[0]['setup']>): {
    readonly hosts: Hosts;
    readonly settings: SettingsStore;
    readonly manager: ConnectionManager;
  } {
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    });
    const manager = new ConnectionManager({
      linkFor: () => {
        throw new Error('the helper must stay closed');
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    return {
      settings,
      manager,
      hosts: hostsInWorld({
        manager,
        settings,
        localCheckout: '/bundled',
        sshConfigPath: '/missing',
        log: () => {},
        push: () => {},
        retire: () => {},
        setup,
      }),
    };
  }

  test('a host whose VibeSys predates the registry cannot complete paths or take a checkout, and says it needs an update', async () => {
    const {hosts, manager} = v2Hosts(registrySetup({unary: olderHost.read}));
    await using _manager = manager;
    await expect(hosts.complete(LOCAL, '/manual/')).resolves.toEqual({kind: 'unsupported'});
    await expect(
      hosts.setCheckout(LOCAL, '/candidate', {owner: 'doc', sequence: 1}),
    ).rejects.toThrow("This host's VibeSys needs an update");
  });

  test('saves a found compatible checkout without starting the helper', async () => {
    const {hosts, settings, manager} = v2Hosts({
      completePaths: async () => {
        throw new Error('unused');
      },
      inspectCandidate: async (_id, path, revision) => {
        if (revision !== 1n) throw new Error('stale selection');
        return {
          state: 'found',
          root: path,
          version: '0.1.1',
          commit: null,
          dirty: null,
          compatible: true,
        };
      },
    });
    await using _manager = manager;
    await expect(hosts.setCheckout(LOCAL, '/candidate', {owner: 'doc', sequence: 1})).resolves.toBe(
      '/candidate',
    );
    expect((await settings.hosts()).settings.hosts['local']?.checkout).toBe('/candidate');
  });

  test('a cancel during inspection does not save the checkout', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>(resolve => {
      release = resolve;
    });
    let started: () => void = () => {};
    const startedPromise = new Promise<void>(resolve => {
      started = resolve;
    });
    const {hosts, settings, manager} = v2Hosts({
      completePaths: async () => {
        throw new Error('unused');
      },
      inspectCandidate: async (_id, _path, _revision, signal) => {
        started();
        await gate;
        if (signal.aborted) throw new Error('operation cancelled');
        return {
          state: 'found',
          root: '/candidate',
          version: '0.1.1',
          commit: null,
          dirty: null,
          compatible: true,
        };
      },
    });
    await using _manager = manager;
    const pending = hosts.setCheckout(LOCAL, '/candidate', {owner: 'doc', sequence: 1});
    await startedPromise;
    await hosts.cancelCheckout({owner: 'doc', sequence: 1});
    release();
    await expect(pending).rejects.toThrow('the checkout change was cancelled');
    expect((await settings.hosts()).settings.hosts['local']).toBeUndefined();
  });
});

describe('Hosts.readEvents', () => {
  test('returns a typed unsupported outcome without sending runs.read_events', async () => {
    const link = new FakeLink();
    link.helper.features.delete('run_history');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.readEvents(LOCAL, '/srv/project', 'run-1', 0, 10)).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.read_events')).toEqual([]);
  });

  test('names the run when the host has no history for it', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.readEvents(LOCAL, '/srv/project', 'run-1', 0, 10)).rejects.toThrow(
      'Could not read the history of run run-1',
    );
  });
});

describe('Hosts.readEvents page size', () => {
  async function asked(features: 'with' | 'without') {
    const link = new FakeLink();
    if (features === 'without') link.helper.features.delete('run_history_pages');
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;
    await subject.hosts.readEvents(LOCAL, '/srv/project', 'run-1', 1, 2000).catch(() => undefined);
    return link.helper.requests.filter(request => request.type === 'runs.read_events');
  }

  test('asks a helper with run_history_pages for whole pages and the journal totals', async () => {
    expect(await asked('with')).toMatchObject([{max: 2000, with_totals: true}]);
  });

  test('asks one without it for the page size it serves and no totals', async () => {
    const [request] = await asked('without');
    expect(request).toMatchObject({max: 500});
    expect(request).not.toHaveProperty('with_totals');
  });
});

describe('Hosts.readEvents of one kind', () => {
  const OTHER = ['control', 'agent_output_chunk', 'turn_usage_recorded'];
  const TRACE = RUN_EVENT_TYPES_BY_KINDS.trace;

  /** `length` events numbered from 1 whose types a `seed` picks from the trace's and from the rest. */
  function journalOf(seed: number, length: number): Record<string, unknown>[] {
    const types = [...TRACE, ...OTHER, ...OTHER, ...OTHER];
    let state = seed;
    return Array.from({length}, (_, index) => {
      state = (state * 1_103_515_245 + 12_345) % 2_147_483_648;
      return {sequence: index + 1, type: types[Math.floor(state / 65_536) % types.length]};
    });
  }

  /** The host's pages of `journal`, `max` events at most, and the requests the host received. */
  async function readAll(
    journal: Record<string, unknown>[],
    max: number,
    options: {readonly trace: boolean; readonly feature: boolean},
  ) {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.journals.set('/srv/project run-1', journal);
    if (!options.feature) link.helper.features.delete('run_trace_events');
    const subject = completionSubject(link);
    await using _manager = subject.manager;
    const events: unknown[] = [];
    for (let from = 1, pages = 0; pages <= journal.length + 2; pages += 1) {
      const page = await subject.hosts.readEvents(
        LOCAL,
        '/srv/project',
        'run-1',
        from,
        max,
        options.trace ? 'trace' : undefined,
      );
      if (page.kind !== 'page') throw new Error('the host serves no history');
      events.push(...page.events);
      if (page.complete) break;
      expect(page.next).toBeGreaterThan(from);
      from = page.next;
    }
    const asked = link.helper.requests.filter(request => request.type === 'runs.read_events');
    return {events, asked};
  }

  test('asks a helper with run_trace_events to select them, and sends only those', async () => {
    const journal = journalOf(3, 60);
    const {events, asked} = await readAll(journal, 7, {trace: true, feature: true});
    expect(events).toEqual(journal.filter(event => TRACE.includes(String(event['type']))));
    expect(asked.every(request => 'kinds' in request && request.kinds === 'trace')).toBe(true);
  });

  test('selects them itself from an older helper, which is sent no kinds', async () => {
    const journal = journalOf(3, 60);
    const {events, asked} = await readAll(journal, 7, {trace: true, feature: false});
    expect(events).toEqual(journal.filter(event => TRACE.includes(String(event['type']))));
    expect(asked.some(request => 'kinds' in request)).toBe(false);
  });

  test('reads the same events from either helper, in fewer requests from the one that selects', async () => {
    for (const seed of [1, 2, 3, 5, 8, 13, 21, 34]) {
      const journal = journalOf(seed, 20 + seed * 9);
      for (const max of [1, 4, 25, 2000]) {
        const selecting = await readAll(journal, max, {trace: true, feature: true});
        const older = await readAll(journal, max, {trace: true, feature: false});
        expect(selecting.events).toEqual(older.events);
        expect(selecting.asked.length).toBeLessThanOrEqual(older.asked.length);
      }
    }
  });

  test('a read of every event sends no kinds whatever the helper offers', async () => {
    const journal = journalOf(9, 30);
    const {events, asked} = await readAll(journal, 10, {trace: false, feature: true});
    expect(events).toEqual(journal);
    expect(asked.some(request => 'kinds' in request)).toBe(false);
  });
});

describe('Hosts.readSpan', () => {
  test('does not send a span request to a helper without run_span_events', async () => {
    const link = new FakeLink();
    link.helper.features.delete('run_span_events');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(
      subject.hosts.readSpan(LOCAL, '/srv/project', 'run-1', 'op:turn', 1, 100),
    ).resolves.toEqual({kind: 'unsupported'});
    expect(link.helper.requests.filter(request => request.type === 'runs.events_for_span')).toEqual(
      [],
    );
  });

  test('requests only the selected span with a bounded page', async () => {
    const link = new FakeLink();
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await subject.hosts
      .readSpan(LOCAL, '/srv/project', 'run-1', 'op:turn', 13, 100)
      .catch(() => undefined);
    expect(
      link.helper.requests.filter(request => request.type === 'runs.events_for_span'),
    ).toMatchObject([
      {project: '/srv/project', run_id: 'run-1', span_id: 'op:turn', from_sequence: 13, max: 100},
    ]);
  });
});

describe('Hosts.readSummary', () => {
  const tally = (input: number | null) => ({
    input,
    cache_read: null,
    cache_write: null,
    output: null,
    reasoning: null,
  });
  const record = (execution: string, input: number) => ({
    run_id: 'run-1',
    execution_id: execution,
    span_id: null,
    round_label: null,
    role: 'implementer',
    harness: 'codex',
    model: null,
    reasoning_effort: null,
    client: null,
    harness_version: null,
    started_at: null,
    ended_at: null,
    tokens: tally(input),
    cost_usd: null,
    duration_ms: null,
  });
  const page = (through: number, rest: object) => ({
    through_sequence: through,
    event_count: through,
    journal_bytes: 99,
    trace: 'recorded',
    rounds_finished: 0,
    span_count: 2,
    reply_count: 1,
    usage_count: 1,
    record_count: 2,
    tokens: tally(7),
    overview: null,
    input_gates: [],
    terminal: null,
    spans: [],
    replies: [],
    schemas: {},
    usage: [],
    records: [],
    next_offset: null,
    ...rest,
  });
  const key = '/srv/project run-1';
  const SCHEMA = 'a'.repeat(64);

  async function subject(
    pages: readonly {offset: number; page: Record<string, unknown>}[],
    features = true,
  ) {
    const link = new FakeLink();
    if (!features) link.helper.features.delete('run_summary');
    link.helper.host.projects.set('/srv/project', ['mpmc']);
    link.helper.host.summaries.set(key, pages);
    const built = completionSubject(link);
    return {link, ...built};
  }

  test('joins the pages, each reply with the schema its page or an earlier one carried', async () => {
    const {hosts, manager} = await subject([
      {
        offset: 0,
        page: page(40, {
          spans: [{id: 'a'}, {id: 'b'}],
          next_offset: 2,
          schemas: {[SCHEMA]: {title: 'Plan'}},
          records: [record('inv:a', 3)],
        }),
      },
      {
        offset: 2,
        page: page(40, {
          replies: [{span_id: 'a', kind: 'Plan', schema_id: SCHEMA, value: {summary: 'x'}}],
          usage: [{span_id: 'a', tally: tally(5)}],
          records: [record('inv:b', 4)],
        }),
      },
    ]);
    await using _manager = manager;

    const result = await hosts.readSummary(LOCAL, '/srv/project', 'run-1');

    expect(result).toEqual({
      kind: 'summary',
      summary: {
        throughSequence: 40,
        eventCount: 40,
        journalBytes: 99,
        trace: 'recorded',
        roundsFinished: 0,
        overview: null,
        spans: [{id: 'a'}, {id: 'b'}],
        replies: [{spanId: 'a', kind: 'Plan', jsonSchema: {title: 'Plan'}, value: {summary: 'x'}}],
        tokens: {input: 7, cacheRead: null, cacheWrite: null, output: null, reasoning: null},
        usage: [
          {
            spanId: 'a',
            tokens: {input: 5, cacheRead: null, cacheWrite: null, output: null, reasoning: null},
          },
        ],
        records: [record('inv:a', 3), record('inv:b', 4)],
        inputGates: [],
        terminal: null,
      },
    });
  });

  test('carries the input gates and the terminal event, from any page, as the page words them', async () => {
    const gate = {
      sequence: 43,
      gate: 'accuracy',
      recipe: null,
      passed: false,
      output_tail: 'ModuleNotFoundError: No module named x\n',
    };
    const terminal = {
      sequence: 549,
      kind: 'failed' as const,
      message: 'NestedRepositoryError: whole text',
    };
    const {hosts, manager} = await subject([
      {offset: 0, page: page(600, {next_offset: 1, input_gates: [gate], terminal})},
      {offset: 1, page: page(600, {input_gates: [gate], terminal})},
    ]);
    await using _manager = manager;

    const result = await hosts.readSummary(LOCAL, '/srv/project', 'run-1');

    expect(result.kind === 'summary' ? result.summary.inputGates : null).toEqual([
      {
        sequence: 43,
        gate: 'accuracy',
        recipe: null,
        passed: false,
        outputTail: 'ModuleNotFoundError: No module named x\n',
      },
    ]);
    expect(result.kind === 'summary' ? result.summary.terminal : null).toEqual(terminal);
  });

  test('asks for the ending only of a helper with run_ending, and reads a summary without one', async () => {
    const {hosts, link, manager} = await subject([{offset: 0, page: page(1, {})}]);
    await using _manager = manager;
    await hosts.readSummary(LOCAL, '/srv/project', 'run-1');
    link.helper.features.delete('run_ending');
    const before = await hosts.readSummary(LOCAL, '/srv/project', 'run-1');

    const asked = link.helper.requests.filter(request => request.type === 'runs.summary');
    expect(asked).toMatchObject([{with_ending: true}, {}]);
    expect(asked[1]).not.toHaveProperty('with_ending');
    expect(
      before.kind === 'summary' ? [before.summary.inputGates, before.summary.terminal] : null,
    ).toEqual([[], null]);
  });

  test('reads again from the start when the run moved between two pages, and gives up after a few', async () => {
    const moving = await subject([
      {offset: 0, page: page(40, {next_offset: 2})},
      {offset: 2, page: page(41, {})},
    ]);
    await using _manager = moving.manager;

    await expect(moving.hosts.readSummary(LOCAL, '/srv/project', 'run-1')).rejects.toThrow(
      'the run kept changing while its summary was read',
    );
    expect(
      moving.link.helper.requests.filter(request => request.type === 'runs.summary').length,
    ).toBe(6);
  });

  test('is unsupported, and sends nothing, on a helper without run_summary', async () => {
    const {hosts, link, manager} = await subject([{offset: 0, page: page(1, {})}], false);
    await using _manager = manager;

    await expect(hosts.readSummary(LOCAL, '/srv/project', 'run-1')).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.summary')).toEqual([]);
  });

  test('is unsupported, and sends nothing, on a helper whose summary predates its trace', async () => {
    // The pages are what such a helper writes: no `trace` and no `rounds_finished`. Reading one
    // would end the channel, so the machine would look unreachable for good.
    const {trace: _trace, rounds_finished: _rounds, ...before} = page(1, {});
    const {hosts, link, manager} = await subject([{offset: 0, page: before}]);
    link.helper.features.delete('run_trace');
    await using _manager = manager;

    await expect(hosts.readSummary(LOCAL, '/srv/project', 'run-1')).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'runs.summary')).toEqual([]);
    // The machine is still usable for what that helper serves.
  });

  test('says the run is gone when the host holds no record of it', async () => {
    const {hosts, manager} = await subject([]);
    await using _manager = manager;

    await expect(hosts.readSummary(LOCAL, '/srv/project', 'run-9')).resolves.toEqual({
      kind: 'gone',
    });
  });
});

/** The registered reads for digests and the usage window, answering what the test says. */
class FakeRunReads implements RegistryRunReads {
  readonly digested: {readonly project: string; readonly runIds: readonly string[]}[] = [];
  readonly windows: {
    readonly runIds: readonly string[];
    readonly since: number;
    readonly until: number;
  }[] = [];
  failure: Error | null = null;

  async digests(_host: HostId, project: string, runIds: readonly string[]) {
    this.digested.push({project, runIds});
    if (this.failure !== null) throw this.failure;
    return {kind: 'digests' as const, runs: new Map()};
  }

  async usageWindow(
    _host: HostId,
    _project: string,
    runIds: readonly string[],
    since: number,
    until: number,
  ) {
    this.windows.push({runIds, since, until});
    if (this.failure !== null) throw this.failure;
    return {kind: 'unsupported' as const};
  }
}

/** Hosts whose helper cannot start: digests and the usage window must not need it. */
function runReadsSubject() {
  const reads = new FakeRunReads();
  let helperAcquisitions = 0;
  const manager = new ConnectionManager({
    linkFor: () => {
      helperAcquisitions += 1;
      throw new Error('helper unavailable; gateway still available');
    },
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  const hosts = hostsInWorld({
    manager,
    settings,
    localCheckout: '/bundled',
    sshConfigPath: '/missing',
    runReads: reads,
    log: () => {},
    push: () => {},
    retire: () => {},
  });
  return {hosts, reads, manager, acquisitions: () => helperAcquisitions};
}

describe('Hosts.runDigests', () => {
  test("asks the host's registered read for the named runs and never its helper", async () => {
    const {hosts, reads, manager, acquisitions} = runReadsSubject();
    await using _manager = manager;

    await expect(hosts.runDigests(LOCAL, '/srv/project', ['run-1'])).resolves.toEqual({
      kind: 'digests',
      runs: new Map(),
    });

    expect(reads.digested).toEqual([{project: '/srv/project', runIds: ['run-1']}]);
    expect(acquisitions()).toBe(0);
  });

  test('a host without the read answers a typed unsupported outcome', async () => {
    const {hosts, reads, manager} = runReadsSubject();
    await using _manager = manager;
    reads.failure = new OperationFailed(FailureCategory.UNSUPPORTED, 'older checkout');

    await expect(hosts.runDigests(LOCAL, '/srv/project', ['run-1'])).resolves.toEqual({
      kind: 'unsupported',
    });
  });

  test('any other failure reaches the caller unchanged', async () => {
    const {hosts, reads, manager} = runReadsSubject();
    await using _manager = manager;
    reads.failure = new Error('project missing');

    await expect(hosts.runDigests(LOCAL, '/srv/nowhere', [])).rejects.toThrow('project missing');
  });
});

describe('Hosts.usageWindow', () => {
  const SINCE = Date.parse('2026-10-17T00:00:00Z');
  const UNTIL = Date.parse('2026-10-18T00:00:00Z');

  test('asks the registered read for the window in milliseconds and never the helper', async () => {
    const {hosts, reads, manager, acquisitions} = runReadsSubject();
    await using _manager = manager;

    await hosts.usageWindow(LOCAL, '/srv/project', ['run-1', 'run-9'], SINCE, UNTIL);

    expect(reads.windows).toEqual([{runIds: ['run-1', 'run-9'], since: SINCE, until: UNTIL}]);
    expect(acquisitions()).toBe(0);
  });

  test('a host without the read answers a typed unsupported outcome', async () => {
    const {hosts, reads, manager} = runReadsSubject();
    await using _manager = manager;
    reads.failure = new OperationFailed(FailureCategory.UNSUPPORTED, 'older checkout');

    await expect(hosts.usageWindow(LOCAL, '/srv/project', [], SINCE, UNTIL)).resolves.toEqual({
      kind: 'unsupported',
    });
  });

  test('property: a window or run list the host would refuse is refused here, and only that', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.nat({max: 120}),
        fc.integer({min: -10, max: 400}),
        fc.integer({min: -10, max: 400}),
        async (count, sinceHours, untilHours) => {
          const {hosts, reads, manager} = runReadsSubject();
          await using _manager = manager;
          const ids = Array.from({length: count}, (_, at) => `run-${at}`);
          const since = SINCE + sinceHours * 3_600_000;
          const until = SINCE + untilHours * 3_600_000;
          const refused = usageWindowRefusal(ids, since, until) !== null;

          const asked = hosts.usageWindow(LOCAL, '/srv/project', ids, since, until);

          if (refused) await expect(asked).rejects.toThrow();
          else await asked;
          expect(reads.windows).toHaveLength(refused ? 0 : 1);
          expect(refused).toBe(
            count > 100 || untilHours <= sinceHours || untilHours - sinceHours > 336,
          );
        },
      ),
      {numRuns: 80, seed: 4313},
    );
  });
});

describe('Hosts checkout changes', () => {
  test('removing a host fences an older checkout validation before acknowledging removal', async () => {
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    const peer = new FakeReadCatalog();
    const held = peer.deferInspection('/new');
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld(
      {
        manager,
        settings,
        localCheckout: '/bundled',
        sshConfigPath: '/missing',
        log: () => {},
        push: () => {},
        retire: () => {},
      },
      peer,
    );
    const change = {owner: '10:100', sequence: 1};
    const changing = hosts.setCheckout(ALPHA, '/new', change);
    await held.received;

    const removing = hosts.remove(ALPHA);
    await expect(
      hosts.setCheckout(ALPHA, '/newer', {owner: '10:100', sequence: 2}),
    ).rejects.toThrow('alpha is being removed');
    held.release();

    await expect(changing).rejects.toThrow('checkout change was cancelled');
    await removing;
    await hosts.cancelCheckout(change);
    expect(settingsFor((await settings.hosts()).settings, ALPHA)).toBeNull();
  });

  test('removing a host wins after an older checkout has crossed its commit boundary', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = filesystem.deferNextReplaceAfterCommit();
    const changing = hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    await held.committed;

    const removing = hosts.remove(ALPHA);
    held.release();

    await expect(changing).resolves.toBe('/src/vibesys');
    await removing;
    expect(settingsFor((await settings.hosts()).settings, ALPHA)).toBeNull();
    expect(manager.existing('ssh:alpha')).toBeNull();
  });

  test('an ambiguous removal write still retires the watcher matching its visible result', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const world = await gatewayWorld({filesystem, configured: [ALPHA], failingCallbacks: true});
    await world.hosts.recent([ALPHA]);
    expect(world.watched()).toEqual(['ssh:alpha']);
    const persistence = new Error('directory sync failed');
    filesystem.failNextDirectorySync(settingsBinaryPath('/settings/hosts.json'), persistence);

    await expect(world.hosts.remove(ALPHA)).rejects.toBe(persistence);

    expect(settingsFor((await world.settings.hosts()).settings, ALPHA)).toBeNull();
    expect(world.watched()).toEqual([]);
    expect(world.retired).toEqual(['ssh:alpha']);
  });

  test('a failed removal preserves its persistence error when repair diagnostics also fail', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {
        throw new Error('diagnostic failed');
      },
      push: () => {},
      retire: () => {},
    });
    await hosts.recent([ALPHA]);
    await manager[Symbol.asyncDispose]();
    const persistence = new Error('replace failed');
    filesystem.failNextReplace(settingsBinaryPath('/settings/hosts.json'), persistence);

    await expect(hosts.remove(ALPHA)).rejects.toBe(persistence);

    expect(settingsFor((await settings.hosts()).settings, ALPHA)?.checkout).toBe('/old');
  });

  test('cancelling checkout validation cannot persist or replace a newer operation', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const world = await gatewayWorld({filesystem, configured: [LOCAL]});
    await world.hosts.host(LOCAL, '10:100');
    const first = world.gateway.open(LOCAL);

    const held = filesystem.deferNextWrite();
    const obsolete = world.hosts.setCheckout(LOCAL, '/cancelled', {owner: '10:100', sequence: 1});
    await held.received;

    // A reloaded renderer may reuse sequence 1, but its document owner keeps the operation unique.
    const newer = world.hosts.setCheckout(LOCAL, '/newer', {owner: '11:101', sequence: 1});
    const retired = world.hosts.cancelCheckoutOwner('10:100');
    held.release();
    await expect(obsolete).rejects.toThrow('checkout change was cancelled');
    await retired;
    await expect(newer).resolves.toBe('/src/vibesys');

    expect(settingsFor((await world.settings.hosts()).settings, LOCAL)?.checkout).toBe('/newer');
    // The cancelled candidate never replaced the watcher; the newer checkout replaced it once.
    expect(first.every(stream => stream.signal.aborted)).toBe(true);
    expect(world.openStreams('local')).toBe(1);
    world.gateway.announce(LOCAL, []);
    await world.settled();
    expect(world.checked().at(-1)).toBe('/newer');
  });

  test('a cancellation after atomic replace reports and installs the committed checkout', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const world = await gatewayWorld({filesystem, configured: [LOCAL]});
    await world.hosts.host(LOCAL, '10:100');
    const first = world.gateway.open(LOCAL);

    const held = filesystem.deferNextReplaceAfterCommit();
    const change = {owner: '10:100', sequence: 1};
    const changing = world.hosts.setCheckout(LOCAL, '/new', change);
    await held.committed;
    await world.hosts.cancelCheckout(change);
    held.release();

    await expect(changing).resolves.toBe('/src/vibesys');
    expect(settingsFor((await world.settings.hosts()).settings, LOCAL)?.checkout).toBe('/new');
    expect(first.every(stream => stream.signal.aborted)).toBe(true);
    expect(world.openStreams('local')).toBe(1);
    world.gateway.announce(LOCAL, []);
    await world.settled();
    expect(world.checked().at(-1)).toBe('/new');
  });
});

describe('Hosts.connection racing a checkout change', () => {
  test('a checkout read captured before commit cannot acquire the stale checkout afterward', async () => {
    const filesystem = new CapturedReadFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const held = filesystem.deferNextRead();
    const acquiring = hosts.connection(ALPHA);
    await held.captured;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    held.release();

    const lease = await acquiring;
    expect(lease.connection.where).toBe('/new on fake');
    expect(manager.existing('ssh:alpha')).toBe(lease.connection);
    // The stale checkout was never acquired, so there is nothing of it to release.
    expect(manager.releasedCheckouts).toEqual([]);
    await lease[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')).toBeNull();
  });

  test.each([
    'before',
    'after',
  ] as const)('an acquisition superseded %s manager delivery disposes once and retries current', async position => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    const link = new FakeLink();
    await using manager = new DeferredAcquireManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const held = manager.deferNext('/old', position);
    const acquiring = hosts.connection(ALPHA);
    await held.received;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    held.release();

    const lease = await acquiring;
    expect(lease.connection.where).toBe('/new on fake');
    expect(manager.existing('ssh:alpha')).toBe(lease.connection);
    // The stale lease is released once, on either side of the manager's delivery, and no watcher
    // holds a connection of its own to release beside it.
    const releasedBeforeReturn = ['/old'];
    expect(manager.releasedCheckouts).toEqual(releasedBeforeReturn);
    await lease[Symbol.asyncDispose]();
    await lease[Symbol.asyncDispose]();
    expect(manager.releasedCheckouts).toEqual([...releasedBeforeReturn, '/new']);
    expect(manager.existing('ssh:alpha')).toBeNull();
  });

  test('a superseded acquisition repairs its host without waiting for an unrelated host', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current => withHostSettings(current, BETA, {checkout: '/beta'}));
    await settings.updateHosts(current => withHostSettings(current, ALPHA, {checkout: '/old'}));
    await using manager = new DeferredAcquireManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.recent([BETA, ALPHA]);

    const heldAlpha = manager.deferNext('/old');
    const acquiringAlpha = hosts.connection(ALPHA);
    await heldAlpha.received;
    await hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});
    const displacedBeta = await manager.acquire(BETA, '/other');
    await displacedBeta[Symbol.asyncDispose]();
    expect(manager.existing('ssh:beta')).toBeNull();
    const heldBeta = manager.deferNext('/beta', 'before');

    heldAlpha.release();
    const winner = await Promise.race([
      acquiringAlpha.then(() => 'alpha' as const),
      heldBeta.received.then(() => 'beta' as const),
    ]);
    heldBeta.release();
    const lease = await acquiringAlpha;

    expect(winner).toBe('alpha');
    expect(lease.connection.where).toBe('/new on fake');
    expect(manager.existing('ssh:alpha')).toBe(lease.connection);
    expect(manager.releasedCheckouts.filter(checkout => checkout === '/old')).toHaveLength(1);
    await lease[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')).toBeNull();
  });

  test('a non-desired checkout commit does not disturb another host being watched', async () => {
    const filesystem = new CapturedReadFilesystem();
    const world = await gatewayWorld({filesystem, configured: [ALPHA, BETA]});
    const heldAlpha = filesystem.deferNextRead();
    const reconcilingAlpha = world.hosts.recent([ALPHA]);
    await heldAlpha.captured;
    await world.hosts.setCheckout(BETA, '/new-beta', {owner: 'retired', sequence: 1});
    expect(world.watched()).toEqual([]);
    heldAlpha.release();
    await reconcilingAlpha;

    expect(world.watched()).toEqual(['ssh:alpha']);
    expect(settingsFor((await world.settings.hosts()).settings, BETA)?.checkout).toBe('/new-beta');
  });
});

/** The ops a renderer and the settings page can issue against the desired set. */
type DesiredOp =
  | {readonly kind: 'recent'; readonly hosts: readonly number[]}
  | {readonly kind: 'host'; readonly host: number; readonly owner: number}
  | {readonly kind: 'retire'; readonly owner: number}
  | {readonly kind: 'remove'; readonly host: number};

const WORLD_HOSTS: readonly HostId[] = [LOCAL, ALPHA, BETA];

type GatewayStep =
  | {readonly kind: 'announce'; readonly runs: readonly {id: string; liveness: RunLiveness}[]}
  | {readonly kind: 'fail'}
  | {readonly kind: 'advance'; readonly ms: number};

/** One thing the host's gateway does. */
async function playGateway(
  world: Awaited<ReturnType<typeof gatewayWorld>>,
  step: GatewayStep,
): Promise<void> {
  switch (step.kind) {
    case 'announce':
      world.gateway.announce(
        LOCAL,
        step.runs.map(run => ({runId: run.id, projectRoot: '/project', liveness: run.liveness})),
      );
      return;
    case 'fail':
      await world.failStreams(LOCAL);
      return;
    case 'advance':
      world.clock.advanceBy(step.ms);
  }
}

/** What the desired set is, written from the product's rules and not from its code. */
class DesiredModel {
  #recent = new Set<number>();
  #visible: {owner: number; host: number} | null = null;
  readonly #removed = new Set<number>();

  async apply(hosts: Hosts, op: DesiredOp): Promise<void> {
    switch (op.kind) {
      case 'recent':
        this.#recent = new Set(op.hosts);
        await hosts.recent(op.hosts.map(index => WORLD_HOSTS[index] as HostId));
        return;
      case 'host':
        this.#visible = {owner: op.owner, host: op.host};
        await hosts.host(WORLD_HOSTS[op.host] as HostId, `owner-${op.owner}`);
        return;
      case 'retire':
        if (this.#visible?.owner === op.owner) this.#visible = null;
        await hosts.retireOwner(`owner-${op.owner}`);
        return;
      case 'remove':
        this.#removed.add(op.host);
        await hosts.remove(WORLD_HOSTS[op.host] as HostId);
    }
  }

  /** The keys of the hosts that should be watched. A removed machine has no checkout left. */
  watched(): string[] {
    const desired = new Set([
      ...this.#recent,
      ...(this.#visible === null ? [] : [this.#visible.host]),
    ]);
    return [...desired]
      .filter(index => index === 0 || !this.#removed.has(index))
      .map(index => hostKey(WORLD_HOSTS[index] as HostId))
      .sort();
  }
}

describe('Hosts watch the gateway of every desired host', () => {
  test('a gateway that can list lists runs although its helper cannot start (the reviewer repro)', async () => {
    const world = await gatewayWorld({helper: 'down', configured: []});
    await world.hosts.recent([LOCAL]);
    world.gateway.announce(LOCAL, [
      {runId: 'run-a', projectRoot: '/project', liveness: RunLiveness.ALIVE},
    ]);

    const last = world.pushed.at(-1);
    if (last?.kind !== 'listed') throw new Error('listed required');
    expect(last.live.map(run => run.runId)).toEqual(['run-a']);
    expect(world.helperAcquisitions()).toBe(0);
    expect(world.logged).toEqual([]);
  });

  const SCRIPT = fc.array(
    fc.oneof(
      fc.record({
        kind: fc.constant('announce' as const),
        runs: fc.array(
          fc.record({
            id: fc.constantFrom('run-a', 'run-b', 'run-c'),
            liveness: fc.constantFrom(
              RunLiveness.ALIVE,
              RunLiveness.INTERRUPTED,
              RunLiveness.ENDED,
              RunLiveness.UNSPECIFIED,
            ),
          }),
          {maxLength: 3},
        ),
      }),
      fc.record({kind: fc.constant('fail' as const)}),
      fc.record({kind: fc.constant('advance' as const), ms: fc.integer({min: 0, max: 40_000})}),
    ),
    {maxLength: 8},
  );

  test('property: a host listing and its status depend only on its gateway, never on its helper', async () => {
    await fc.assert(
      fc.asyncProperty(SCRIPT, async script => {
        const outcomes = [];
        for (const helper of ['up', 'down'] as const) {
          const world = await gatewayWorld({helper, configured: []});
          await world.hosts.recent([LOCAL]);
          for (const step of script) await playGateway(world, step);
          expect(world.helperAcquisitions()).toBe(0);
          outcomes.push(world.pushed);
        }
        expect(outcomes[1]).toEqual(outcomes[0]);
      }),
      {numRuns: 40, seed: 4311},
    );
  });

  test('status follows the stream: connecting, then listed, reconnecting after a failure, listed again', async () => {
    const world = await gatewayWorld({helper: 'down', configured: []});
    await world.hosts.recent([LOCAL]);
    const kinds = () =>
      world.pushed.map(listing => (listing.kind === 'listed' ? 'listed' : listing.connection.kind));
    expect(kinds()).toEqual(['connecting']);
    world.gateway.announce(LOCAL, []);
    await world.failStreams(LOCAL);
    expect(kinds()).toEqual(['connecting', 'listed', 'reconnecting']);
    world.clock.advanceBy(1_000);
    world.gateway.announce(LOCAL, [
      {runId: 'run-a', projectRoot: '/project', liveness: RunLiveness.ENDED},
    ]);
    expect(kinds()).toEqual(['connecting', 'listed', 'reconnecting', 'listed']);
  });

  const OPS = fc.array(
    fc.oneof(
      fc.record({
        kind: fc.constant('recent' as const),
        hosts: fc.uniqueArray(fc.nat({max: 2}), {maxLength: 3}),
      }),
      fc.record({
        kind: fc.constant('host' as const),
        host: fc.nat({max: 2}),
        owner: fc.nat({max: 1}),
      }),
      fc.record({kind: fc.constant('retire' as const), owner: fc.nat({max: 1})}),
      fc.record({kind: fc.constant('remove' as const), host: fc.nat({max: 2})}),
    ),
    {maxLength: 10},
  );

  test('property: the hosts with an open stream are exactly the desired hosts, each once', async () => {
    await fc.assert(
      fc.asyncProperty(OPS, async (ops: readonly DesiredOp[]) => {
        const world = await gatewayWorld({configured: [ALPHA, BETA]});
        const model = new DesiredModel();
        for (const op of ops) {
          await model.apply(world.hosts, op);
          const expected = model.watched();
          expect(world.watched()).toEqual(expected);
          for (const key of expected) expect(world.openStreams(key)).toBe(1);
        }
      }),
      {numRuns: 40, seed: 4312},
    );
  });

  test('switching panels releases a host absent from the bounded recent set', async () => {
    const world = await gatewayWorld({configured: [ALPHA, BETA]});
    await world.hosts.host(ALPHA, '10:100');
    await world.hosts.host(BETA, '10:100');
    expect(world.watched()).toEqual(['ssh:beta']);
    expect(world.retired).toEqual(['ssh:alpha']);
    expect(world.pushed.filter(listing => listing.host === 'ssh:beta').at(0)).toEqual({
      kind: 'unavailable',
      host: 'ssh:beta',
      connection: {kind: 'connecting'},
    });
  });

  test('recent and visible intent form one set, independently of update order', async () => {
    const world = await gatewayWorld({configured: [ALPHA, BETA]});
    await world.hosts.host(ALPHA, '10:100');
    const alpha = world.gateway.open(ALPHA);
    await world.hosts.recent([ALPHA, BETA]);
    await world.hosts.host(BETA, '10:100');
    expect(world.gateway.open(ALPHA)).toEqual(alpha);

    await world.hosts.recent([]);
    expect(world.watched()).toEqual(['ssh:beta']);
    expect(alpha.every(stream => stream.signal.aborted)).toBe(true);
  });

  test('a checkout change replaces the watcher and the host is checked from the committed checkout', async () => {
    const world = await gatewayWorld({configured: [ALPHA]});
    await world.hosts.host(ALPHA, '10:100');
    world.gateway.announce(ALPHA, []);
    const old = world.gateway.open(ALPHA);
    await world.hosts.setCheckout(ALPHA, '/new', {owner: '10:100', sequence: 1});

    expect(old.every(stream => stream.signal.aborted)).toBe(true);
    expect(world.openStreams('ssh:alpha')).toBe(1);
    world.gateway.announce(ALPHA, []);
    await world.settled();
    expect(world.checked().at(-1)).toBe('/new');
  });

  test('reconciliation replaces a watcher whose checkout differs from authoritative settings', async () => {
    const world = await gatewayWorld({configured: [ALPHA]});
    await world.hosts.host(ALPHA, '10:100');
    const old = world.gateway.open(ALPHA);
    await world.settings.updateHosts(current =>
      withHostSettings(current, ALPHA, {checkout: '/authoritative'}),
    );

    await world.hosts.recent([ALPHA]);

    expect(old.every(stream => stream.signal.aborted)).toBe(true);
    world.gateway.announce(ALPHA, []);
    await world.settled();
    expect(world.checked().at(-1)).toBe('/authoritative');
  });

  test('a late checkout read cannot reinstall an obsolete visible host or listing', async () => {
    const filesystem = new DeferredReadFilesystem({
      '/settings/hosts.json': JSON.stringify({
        version: 2,
        hosts: {
          'ssh:alpha': {checkout: '/checkouts/alpha'},
          'ssh:beta': {checkout: '/checkouts/beta'},
        },
      }),
    });
    const world = await gatewayWorld({filesystem, configured: []});
    const obsolete = world.hosts.host(ALPHA, '10:100');
    await filesystem.received;
    await world.hosts.host(BETA, '10:100');
    filesystem.release();
    await obsolete;

    expect(world.watched()).toEqual(['ssh:beta']);
    expect(new Set(world.pushed.map(listing => listing.host))).toEqual(new Set(['ssh:beta']));
    expect(world.pushed.at(0)).toEqual({
      kind: 'unavailable',
      host: 'ssh:beta',
      connection: {kind: 'connecting'},
    });
  });

  test('a checkout refresh finishes the desired set after fencing an older recent read', async () => {
    const filesystem = new DeferredReadFilesystem(
      {
        '/settings/hosts.json': JSON.stringify({
          version: 2,
          hosts: {
            'ssh:alpha': {checkout: '/checkouts/alpha'},
            'ssh:beta': {checkout: '/checkouts/beta'},
          },
        }),
      },
      2,
    );
    const world = await gatewayWorld({filesystem, configured: []});
    await world.hosts.host(ALPHA, '10:100');

    const olderRecent = world.hosts.recent([ALPHA, BETA]);
    await filesystem.received;
    await world.hosts.setCheckout(ALPHA, '/checkouts/alpha-new', {owner: '10:100', sequence: 1});
    expect(world.watched()).toEqual(['ssh:alpha', 'ssh:beta']);
    filesystem.release();
    await olderRecent;
    expect(world.watched()).toEqual(['ssh:alpha', 'ssh:beta']);
  });

  test('one host that cannot be watched does not stop the others being reconciled', async () => {
    const world = await gatewayWorld({configured: [ALPHA, BETA], refuse: 'ssh:alpha'});
    await world.hosts.recent([ALPHA, BETA]);

    expect(world.watched()).toEqual(['ssh:beta']);
    expect(world.logged).toHaveLength(1);
    expect(world.logged[0]).toContain('alpha');
  });

  test('a watcher whose stop fails is retired once, and reported once', async () => {
    const world = await gatewayWorld({configured: [ALPHA, BETA], failStopOnce: 'ssh:alpha'});
    await world.hosts.host(ALPHA, '10:100');
    await world.hosts.host(BETA, '10:100');

    expect(world.watched()).toEqual(['ssh:beta']);
    expect(world.retired).toEqual(['ssh:alpha']);
    expect(world.logged).toEqual(['could not retire alpha connection: stop failed']);
  });

  describe('configured machines', () => {
    test.each([
      [[ALPHA]],
      [[BETA]],
      [[ALPHA, BETA]],
    ] as const)('every configured machine is watched with no recent run: %j', async configured => {
      const world = await gatewayWorld({configured});
      await world.hosts.configured();
      expect(world.watched()).toEqual(configured.map(hostKey).sort());
    });

    test('nothing is watched when no machine is configured', async () => {
      const world = await gatewayWorld({configured: []});
      await world.hosts.configured();
      expect(world.watched()).toEqual([]);
    });

    test('a removed machine stops being watched and a re-read cannot bring it back', async () => {
      const world = await gatewayWorld({configured: [ALPHA, BETA]});
      await world.hosts.configured();
      await world.hosts.remove(ALPHA);
      expect(world.watched()).toEqual(['ssh:beta']);
      await world.hosts.configured();
      expect(world.watched()).toEqual(['ssh:beta']);
    });
  });
});

describe('Hosts inspection changes', () => {
  test('a newer completed inspection keeps its locator when the older reply arrives last', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateRecent(current =>
      remember(current, {
        host: 'local',
        project: '/p',
        task: null,
        runId: 'run-1',
        instanceId: null,
        attachedAt: 1,
      }),
    );
    const link = new FakeLink();
    const firstInstance = '000000000001';
    const secondInstance = '000000000002';
    link.helper.publish(record(firstInstance, 'run-1'));
    link.helper.publish(record(secondInstance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');

    const first = hosts.inspect(LOCAL, firstInstance, {owner: '10:100', sequence: 1});
    await held.received;
    await hosts.inspect(LOCAL, secondInstance, {owner: '10:100', sequence: 2});
    held.release();
    await first;

    expect((await settings.recent()).runs[0]?.instanceId).toBe(secondInstance);
  });

  test('retiring a renderer document revokes its pending inspection at the commit boundary', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateRecent(current =>
      remember(current, {
        host: 'local',
        project: '/p',
        task: null,
        runId: 'run-1',
        instanceId: null,
        attachedAt: 1,
      }),
    );
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');

    const inspecting = hosts.inspect(LOCAL, instance, {owner: '10:100', sequence: 1});
    await held.received;
    hosts.retireInspectionOwner('10:100');
    held.release();
    await inspecting;

    expect((await settings.recent()).runs[0]?.instanceId).toBeNull();
    await expect(hosts.inspect(LOCAL, instance, {owner: '10:100', sequence: 2})).rejects.toThrow(
      'inspection owner was retired',
    );
  });

  test('closing an inspector revokes only its pending locator write', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      preferencesPath: join(directory, 'preferences.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateRecent(current =>
      remember(current, {
        host: 'local',
        project: '/p',
        task: null,
        runId: 'run-1',
        instanceId: null,
        attachedAt: 1,
      }),
    );
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = hostsInWorld({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');
    const change = {owner: '10:100', sequence: 1};

    const inspecting = hosts.inspect(LOCAL, instance, change);
    await held.received;
    hosts.cancelInspection(change);
    held.release();
    await inspecting;

    expect((await settings.recent()).runs[0]?.instanceId).toBeNull();
    await hosts.inspect(LOCAL, instance, {owner: change.owner, sequence: 2});
    expect((await settings.recent()).runs[0]?.instanceId).toBe(instance);
  });

  test('an inspection asks the helper for no server log', async () => {
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = inspectorHosts(manager);

    await hosts.inspect(LOCAL, instance, {owner: '10:100', sequence: 1});

    const inspections = link.helper.requests.filter(
      request => request.type === 'instances.inspect',
    );
    expect(inspections).toHaveLength(1);
    expect(inspections[0]).not.toHaveProperty('include_log');
  });

  test("the inspector lists and reads a run's logs over one logs channel, released on close", async () => {
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    link.helper.logStore.open(instance, '000001', {kind: 'run_narration'}, 'vibesys run');
    link.helper.logStore.write(instance, '000001', new TextEncoder().encode('hello\n'));
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = inspectorHosts(manager);

    const listed = await hosts.logs(LOCAL, instance, {kind: 'list'}, '10:100');
    const read = await hosts.logs(
      LOCAL,
      instance,
      {
        kind: 'read',
        token: 'l1',
        read: {
          instance_id: instance,
          stream_id: '000001',
          start: {tail_bytes: 1024},
          max_bytes: 4096,
          wait_ms: 0,
        },
      },
      '10:100',
    );

    expect(listed?.ok && 'streams' in listed && listed.streams.map(stream => stream.id)).toEqual([
      '000001',
    ]);
    expect(read?.ok && 'chunk' in read && read.chunk.data).toBe('hello\n');
    // The control channel, then the one logs channel both requests shared.
    expect(link.helper.spawned).toBe(2);
    const logsChannel = link.helper.channels[1];
    if (logsChannel === undefined) throw new Error('a logs channel was expected');

    hosts.releaseLogs('10:100');
    await logsChannel.process.exit;
    expect(link.helper.exited).toBeGreaterThanOrEqual(1);
  });

  test("a withdraw aborts the view's read with that token, and withdraws its long poll", async () => {
    const link = new FakeLink();
    const instance = '000000000001';
    link.helper.publish(record(instance, 'run-1'));
    link.helper.logStore.open(instance, '000001', {kind: 'run_narration'}, 'vibesys run');
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = inspectorHosts(manager);
    const owner = '10:100';
    const read = {
      instance_id: instance,
      stream_id: '000001',
      start: {offset: 0},
      max_bytes: 4096,
      wait_ms: 25_000,
    };
    const heldFirst = link.helper.deferNext('logs.read');
    const heldSecond = link.helper.deferNext('logs.read');
    const first = hosts.logs(LOCAL, instance, {kind: 'read', token: 'l1', read}, owner);
    const second = hosts.logs(LOCAL, instance, {kind: 'read', token: 'l2', read}, owner);
    await heldFirst.received;
    await heldSecond.received;

    const withdraw = (token: string, by = owner) =>
      hosts.logs(LOCAL, instance, {kind: 'withdraw', token}, by);
    expect(await withdraw('unknown')).toBeNull();
    expect(await withdraw('l1', '11:100')).toBeNull();
    expect(await withdraw('l1')).toBeNull();
    expect(await first).toMatchObject({ok: false, failure: {code: 'transport'}});
    heldSecond.release();
    expect(await second).toMatchObject({ok: true});
    expect(await withdraw('l2')).toBeNull();

    // The logs channel keeps order: a withdrawal written before this listing reached the helper.
    expect(await hosts.logs(LOCAL, instance, {kind: 'list'}, owner)).toMatchObject({ok: true});
    const [firstRead] = link.helper.requests.filter(request => request.type === 'logs.read');
    const cancels = link.helper.requests.filter(request => request.type === 'logs.cancel');
    expect(cancels).toMatchObject([{read_id: firstRead?.request_id}]);
    heldFirst.release();
    hosts.releaseLogs(owner);
  });
});

function inspectorHosts(manager: ConnectionManager): Hosts {
  return hostsInWorld({
    manager,
    settings: new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      filesystem: new FakeSettingsFilesystem(),
      log: () => {},
    }),
    localCheckout: '/bundled',
    sshConfigPath: '/missing',
    log: () => {},
    push: () => {},
    retire: () => {},
  });
}

test('saved-run discovery always uses the registry', async () => {
  const link = new FakeLink();
  let calls = 0;
  const subject = completionSubject(link, {
    list: async (_host, project, signal) => {
      expect(project).toBe('/srv/project');
      expect(signal.aborted).toBe(false);
      calls += 1;
      return [{runId: 'native-run', name: 'Native run', task: null, createdAt: 1}];
    },
  });
  await using _manager = subject.manager;
  const rows = await subject.hosts.projectRuns(LOCAL, '/srv/project');
  expect(calls).toBe(1);
  expect(link.helper.requests).toHaveLength(0);
  expect(rows[0]?.runId).toBe('native-run');
});
