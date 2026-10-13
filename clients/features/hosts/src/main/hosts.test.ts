import {describe, expect, test} from 'bun:test';
import {join} from 'node:path';
import {ConnectionManager} from '@vibesys/connection';
import {FakeLink, record} from '@vibesys/connection/testing';
import type {HostId, HostListing} from '@vibesys/hosts';
import {remember, SettingsStore, settingsFor, withHostSettings} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {Hosts, type SshConfigFilesystem} from './index.js';

const SSH_CONFIG = '/home/test/.ssh/config';
const LOCAL = {kind: 'local'} as const;
const ALPHA = {kind: 'ssh', alias: 'alpha'} as const;
const BETA = {kind: 'ssh', alias: 'beta'} as const;
const CONFIG = {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000};

/** An explicit SSH-config read result, with every requested path recorded. */
class FakeSshConfigFilesystem implements SshConfigFilesystem {
  readonly reads: string[] = [];

  constructor(readonly result: {readonly text: string} | {readonly error: unknown}) {}

  async readText(path: string): Promise<string> {
    this.reads.push(path);
    if ('error' in this.result) throw this.result.error;
    return this.result.text;
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

  override async readText(path: string): Promise<string> {
    this.#reads += 1;
    if (this.#reads === this.#heldRead) {
      this.#received();
      await this.#released;
    }
    return super.readText(path);
  }

  release(): void {
    this.#release();
  }
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
    log: () => {},
  });
  return new Hosts({
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
function completionSubject(link: FakeLink): {
  readonly hosts: Hosts;
  readonly manager: ConnectionManager;
} {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
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
    hosts: new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    }),
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
  test('returns a typed unsupported outcome without sending paths.complete', async () => {
    const link = new FakeLink();
    link.helper.features.delete('paths_complete');
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.complete(LOCAL, '/manual/')).resolves.toEqual({
      kind: 'unsupported',
    });
    expect(link.helper.requests.filter(request => request.type === 'paths.complete')).toEqual([]);
  });

  test('returns a typed listing when paths_complete was negotiated', async () => {
    const link = new FakeLink();
    link.helper.host.directories.set('/manual', ['project']);
    const subject = completionSubject(link);
    await using _manager = subject.manager;

    await expect(subject.hosts.complete(LOCAL, '/manual/')).resolves.toEqual({
      kind: 'listed',
      directory: '/manual',
      entries: [
        {
          name: 'project',
          path: '/manual/project',
          checkout: false,
          project: false,
        },
      ],
      truncated: false,
    });
    expect(link.helper.requests.filter(request => request.type === 'paths.complete')).toHaveLength(
      1,
    );
  });
});

describe('Hosts checkout changes', () => {
  test('cancelling checkout validation cannot persist or replace a newer operation', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current =>
      withHostSettings(current, LOCAL, {checkout: '/existing'}),
    );
    const link = new FakeLink();
    const clock = new FakeClock();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: clock.scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(LOCAL, '10:100');
    await manager.existing('local')?.ready();

    const held = filesystem.deferNextWrite();
    const obsolete = hosts.setCheckout(LOCAL, '/cancelled', {owner: '10:100', sequence: 1});
    await held.received;

    // A reloaded renderer may reuse sequence 1, but its document owner keeps the operation unique.
    const newer = hosts.setCheckout(LOCAL, '/newer', {owner: '11:101', sequence: 1});
    const retired = hosts.cancelCheckoutOwner('10:100');
    held.release();
    await expect(obsolete).rejects.toThrow('checkout change was cancelled');
    await retired;
    await expect(newer).resolves.toBe('/src/vibesys');

    expect(settingsFor((await settings.hosts()).settings, LOCAL)?.checkout).toBe('/newer');
    // `/cancelled` opened only its disposable candidate; it never replaced the watched connection.
    expect(link.helper.checkouts).toEqual(['/existing', '/cancelled', '/newer', '/newer']);
    expect(manager.existing('local')?.checkoutRoot).toBe('/src/vibesys');
  });

  test('a cancellation after atomic replace reports and installs the committed checkout', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      filesystem,
      log: () => {},
    });
    await settings.updateHosts(current =>
      withHostSettings(current, LOCAL, {checkout: '/existing'}),
    );
    const link = new FakeLink();
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(LOCAL, '10:100');
    await manager.existing('local')?.ready();

    const held = filesystem.deferNextReplaceAfterCommit();
    const change = {owner: '10:100', sequence: 1};
    const changing = hosts.setCheckout(LOCAL, '/new', change);
    await held.committed;
    await hosts.cancelCheckout(change);
    held.release();

    await expect(changing).resolves.toBe('/src/vibesys');
    expect(settingsFor((await settings.hosts()).settings, LOCAL)?.checkout).toBe('/new');
    expect(link.helper.checkouts).toEqual(['/existing', '/new', '/new']);
    expect(manager.existing('local')?.checkoutRoot).toBe('/src/vibesys');
  });
});

describe('Hosts desired set', () => {
  test('switching panels releases a host absent from the bounded recent set', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const links = new Map<string, FakeLink>();
    await using manager = new ConnectionManager({
      linkFor: host => {
        const link = new FakeLink();
        links.set(host.kind === 'local' ? 'local' : host.alias, link);
        return link;
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const pushed: HostListing[] = [];
    const retired: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: listing => pushed.push(listing),
      retire: host => retired.push(host),
    });

    await hosts.host(ALPHA, '10:100');
    const alphaConnection = manager.existing('ssh:alpha');
    await alphaConnection?.ready();
    await hosts.host(BETA, '10:100');
    await alphaConnection?.[Symbol.asyncDispose]();

    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    expect(links.get('alpha')?.released).toBe(1);
    expect(retired).toEqual(['ssh:alpha']);
    expect(pushed.filter(listing => listing.host === 'ssh:beta').at(0)).toEqual({
      kind: 'unavailable',
      host: 'ssh:beta',
      connection: {kind: 'connecting'},
    });
  });

  test('a connection settled before its host is desired is installed with its real status', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA]);
    const link = new FakeLink();
    link.linkFault = {cause: 'link', detail: 'host unreachable'};
    await using manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      // No backoff slot: the one attempt this test drives goes straight to offline.
      config: {delaysMs: [], deadlineMs: 60_000, heartbeatMs: 15_000},
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    const pushed: HostListing[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: listing => pushed.push(listing),
      retire: () => {},
    });

    // `tasks`, `complete`, and `inspect` each lease a host's connection through `Hosts.connection`,
    // independently of whether the welcome view desires that host yet. A connection their lease
    // settles this way, before the host is desired, must not be relabelled once it is installed:
    // offline has no further phase change coming to ever correct a wrong guess.
    const probe = await hosts.connection(ALPHA);
    await expect(probe.connection.ready()).rejects.toThrow('host unreachable');
    expect(probe.connection.phase.kind).toBe('offline');

    await hosts.host(ALPHA, '10:100');

    expect(pushed.filter(listing => listing.host === 'ssh:alpha')).toEqual([
      {
        kind: 'unavailable',
        host: 'ssh:alpha',
        connection: {kind: 'offline', detail: 'host unreachable'},
      },
    ]);
  });

  test('recent and visible intent form one set, independently of update order', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const links = new Map<string, FakeLink>();
    await using manager = new ConnectionManager({
      linkFor: host => {
        const link = new FakeLink();
        links.set(host.kind === 'local' ? 'local' : host.alias, link);
        return link;
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });

    await hosts.host(ALPHA, '10:100');
    const alpha = manager.existing('ssh:alpha');
    await hosts.recent([ALPHA, BETA]);
    await hosts.host(BETA, '10:100');
    expect(manager.existing('ssh:alpha')).toBe(alpha);

    await hosts.recent([]);
    await alpha?.[Symbol.asyncDispose]();
    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(links.get('alpha')?.released).toBe(1);
    expect(manager.existing('ssh:beta')).not.toBeNull();
  });

  test('a late checkout read cannot reinstall an obsolete visible host or listing', async () => {
    const settingsPath = '/settings/hosts.json';
    const filesystem = new DeferredReadFilesystem({
      [settingsPath]: JSON.stringify({
        version: 2,
        hosts: {
          'ssh:alpha': {checkout: '/checkouts/alpha'},
          'ssh:beta': {checkout: '/checkouts/beta'},
        },
      }),
    });
    const settings = new SettingsStore({
      settingsPath,
      recentPath: '/settings/recent.json',
      filesystem,
      log: () => {},
    });
    const created: string[] = [];
    await using manager = new ConnectionManager({
      linkFor: host => {
        created.push(host.kind === 'local' ? 'local' : host.alias);
        return new FakeLink();
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const pushed: HostListing[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: listing => pushed.push(listing),
      retire: () => {},
    });

    const obsolete = hosts.host(ALPHA, '10:100');
    await filesystem.received;
    await hosts.host(BETA, '10:100');
    filesystem.release();
    await obsolete;

    expect(created).toEqual(['beta']);
    expect(manager.existing('ssh:alpha')).toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    expect(new Set(pushed.map(listing => listing.host))).toEqual(new Set(['ssh:beta']));
    expect(pushed.at(0)).toEqual({
      kind: 'unavailable',
      host: 'ssh:beta',
      connection: {kind: 'connecting'},
    });
  });

  test('checkout refresh finishes the desired set after fencing an older recent read', async () => {
    const settingsPath = '/settings/hosts.json';
    const filesystem = new DeferredReadFilesystem(
      {
        [settingsPath]: JSON.stringify({
          version: 2,
          hosts: {
            'ssh:alpha': {checkout: '/checkouts/alpha'},
            'ssh:beta': {checkout: '/checkouts/beta'},
          },
        }),
      },
      2,
    );
    const settings = new SettingsStore({
      settingsPath,
      recentPath: '/settings/recent.json',
      filesystem,
      log: () => {},
    });
    await using manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    await hosts.host(ALPHA, '10:100');

    const olderRecent = hosts.recent([ALPHA, BETA]);
    await filesystem.received;
    await hosts.setCheckout(ALPHA, '/checkouts/alpha-new', {owner: '10:100', sequence: 1});

    expect(manager.existing('ssh:alpha')).not.toBeNull();
    expect(manager.existing('ssh:beta')).not.toBeNull();
    filesystem.release();
    await olderRecent;
    expect(manager.existing('ssh:beta')).not.toBeNull();
  });

  test('one host failing to acquire does not abort reconciling the others', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      filesystem,
      log: () => {},
    });
    await configuredSettings(settings, [ALPHA, BETA]);
    const manager = new ConnectionManager({
      linkFor: () => {
        throw new Error('reconcile must not open a link once the manager is disposed');
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    // The manager disposes (as it does mid-app-shutdown) before either host is acquired, so
    // every acquisition in the pass below rejects the same way a real shutdown race would.
    await manager[Symbol.asyncDispose]();
    const logged: string[] = [];
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: message => logged.push(message),
      push: () => {},
      retire: () => {},
    });

    // Alpha and beta are both newly desired in this one reconcile pass, alpha attempted first.
    await hosts.recent([ALPHA, BETA]);

    expect(logged).toHaveLength(2);
    expect(logged.some(message => message.includes('alpha'))).toBe(true);
    expect(logged.some(message => message.includes('beta'))).toBe(true);
  });
});

describe('Hosts inspection changes', () => {
  test('a newer completed inspection keeps its locator when the older reply arrives last', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
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
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');

    const first = hosts.inspect(LOCAL, firstInstance, false, {owner: '10:100', sequence: 1});
    await held.received;
    await hosts.inspect(LOCAL, secondInstance, false, {owner: '10:100', sequence: 2});
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
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    const held = link.helper.deferNext('instances.inspect');

    const inspecting = hosts.inspect(LOCAL, instance, false, {owner: '10:100', sequence: 1});
    await held.received;
    hosts.retireInspectionOwner('10:100');
    held.release();
    await inspecting;

    expect((await settings.recent()).runs[0]?.instanceId).toBeNull();
    await expect(
      hosts.inspect(LOCAL, instance, false, {owner: '10:100', sequence: 2}),
    ).rejects.toThrow('inspection owner was retired');
  });

  test('closing an inspector revokes only its pending locator write', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const settings = new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
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
      // A released Link is terminal. Preserve the helper's host state while giving each new
      // ConnectionManager generation a fresh transport owner, as production linkFor does.
      linkFor: () => new FakeLink(link.helper),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      config: CONFIG,
      log: () => {},
    });
    const hosts = new Hosts({
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

    const inspecting = hosts.inspect(LOCAL, instance, false, change);
    await held.received;
    hosts.cancelInspection(change);
    held.release();
    await inspecting;

    expect((await settings.recent()).runs[0]?.instanceId).toBeNull();
    await hosts.inspect(LOCAL, instance, false, {owner: change.owner, sequence: 2});
    expect((await settings.recent()).runs[0]?.instanceId).toBe(instance);
  });
});
