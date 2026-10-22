import {describe, expect, test} from 'bun:test';
import {ConnectionManager, registrySetup} from '@vibesys/connection';
import {FakeLink, olderHost, USAGE_DUMP} from '@vibesys/connection/testing';
import {type HostId, type HostListing, hostKey} from '@vibesys/hosts';
import {SettingsStore, withHostSettings} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {checkHostVersion} from './host-version.js';
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
  test('a ready connection is checked once and shown as incompatible, not as its helper listing', async () => {
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
    const hosts = new Hosts({
      manager,
      settings,
      localCheckout: '/bundled',
      sshConfigPath: '/missing',
      transportV2: async () => true,
      setup: registrySetup({unary: olderHost.read}),
      runs: {start: () => {}, cancel: () => {}},
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
    await heard;

    const last = pushed.at(-1);
    expect(last).toMatchObject({
      kind: 'unavailable',
      host: hostKey(ALPHA),
      connection: {kind: 'incompatible'},
    });
    expect(JSON.stringify([pushed, logged])).not.toContain(USAGE_DUMP);
  });

  test('folder completion answers unsupported, not the host text', async () => {
    await using manager = new ConnectionManager({
      linkFor: () => new FakeLink(),
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    });
    const hosts = new Hosts({
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
      transportV2: async () => true,
      setup: registrySetup({unary: olderHost.read}),
      log: () => {},
      push: () => {},
      retire: () => {},
    });
    expect(await hosts.complete(ALPHA, '/work/')).toEqual({kind: 'unsupported'});
  });
});
