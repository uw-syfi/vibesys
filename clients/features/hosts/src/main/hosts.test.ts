import {describe, expect, test} from 'bun:test';
import {join} from 'node:path';
import {ConnectionManager} from '@vibesys/connection';
import {FakeLink, record} from '@vibesys/connection/testing';
import {remember, SettingsStore, settingsFor, withHostSettings} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {Hosts, type SshConfigFilesystem} from './index.js';

const SSH_CONFIG = '/home/test/.ssh/config';
const LOCAL = {kind: 'local'} as const;
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
  });
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
    });
    await hosts.host(LOCAL);
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

    expect(settingsFor(await settings.hosts(), LOCAL)?.checkout).toBe('/newer');
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
    });
    await hosts.host(LOCAL);
    await manager.existing('local')?.ready();

    const held = filesystem.deferNextReplaceAfterCommit();
    const change = {owner: '10:100', sequence: 1};
    const changing = hosts.setCheckout(LOCAL, '/new', change);
    await held.committed;
    await hosts.cancelCheckout(change);
    held.release();

    await expect(changing).resolves.toBe('/src/vibesys');
    expect(settingsFor(await settings.hosts(), LOCAL)?.checkout).toBe('/new');
    expect(link.helper.checkouts).toEqual(['/existing', '/new', '/new']);
    expect(manager.existing('local')?.checkoutRoot).toBe('/src/vibesys');
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
