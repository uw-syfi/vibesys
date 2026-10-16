import {describe, expect, test} from 'bun:test';
import {ConnectionManager} from '@vibesys/connection';
import {FakeLink} from '@vibesys/connection/testing';
import {type HostId, hostKey} from '@vibesys/hosts';
import {SettingsStore, withHostSettings, withoutHost} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {MachinesMain} from './index.js';

const LOCAL: HostId = {kind: 'local'};
const CHELAN4: HostId = {kind: 'ssh', alias: 'chelan4'};
const CHELAN3: HostId = {kind: 'ssh', alias: 'chelan3'};
const CONFIG = {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000};

/** The machines over real settings and a real connection manager, each host behind its own Fake. */
async function subject(links: ReadonlyMap<string, FakeLink>): Promise<{
  readonly machines: MachinesMain;
  readonly settings: SettingsStore;
  readonly manager: ConnectionManager;
}> {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  await settings.updateHosts(current =>
    withHostSettings(withHostSettings(current, CHELAN4, {checkout: '~/vibesys'}), CHELAN3, {
      checkout: '/mnt/data/vibesys',
    }),
  );
  const manager = new ConnectionManager({
    linkFor: id => {
      const link = links.get(hostKey(id));
      if (link === undefined) throw new Error(`no fake link for ${hostKey(id)}`);
      return link;
    },
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    config: CONFIG,
    log: () => {},
  });
  const checkout = async (id: HostId): Promise<string | null> => {
    const saved = (await settings.hosts()).settings.hosts[hostKey(id)]?.checkout ?? null;
    return saved ?? (id.kind === 'local' ? '/Users/me/vibesys' : null);
  };
  const machines = new MachinesMain({
    settings,
    removeHost: id => settings.updateHosts(current => withoutHost(current, id)).then(() => {}),
    checkout,
    connection: async id => {
      const path = await checkout(id);
      if (path === null) throw new Error('no checkout');
      return manager.acquire(id, path);
    },
  });
  return {machines, settings, manager};
}

describe('MachinesMain', () => {
  test('lists This Mac first, then every saved host by name, each with its checkout', async () => {
    const {machines, manager} = await subject(new Map());
    await using _ = manager;
    expect(await machines.machines()).toEqual([
      {host: 'local', label: 'This Mac', checkout: '/Users/me/vibesys'},
      {host: 'ssh:chelan3', label: 'chelan3', checkout: '/mnt/data/vibesys'},
      {host: 'ssh:chelan4', label: 'chelan4', checkout: '~/vibesys'},
    ]);
  });

  test('a machine’s status follows from its connection and its helper’s description', async () => {
    const ready = new FakeLink();
    const missing = new FakeLink();
    missing.helper.bootstrapExit = {code: 65, stdout: 'nodir'};
    const tooOld = new FakeLink();
    tooOld.helper.bootstrapExit = {code: 64, stdout: 'predates 0.0.9'};
    const locked = new FakeLink();
    locked.linkFault = {cause: 'auth', detail: 'the SSH key needs its passphrase'};
    const cases: readonly [FakeLink, string][] = [
      [ready, 'ready'],
      [missing, 'checkout-not-found'],
      [tooOld, 'unavailable'],
      [locked, 'sign-in'],
    ];
    for (const [link, kind] of cases) {
      const {machines, manager} = await subject(new Map([['ssh:chelan4', link]]));
      await using _ = manager;
      const report = await machines.report(CHELAN4);
      expect(report.kind).toBe(kind as typeof report.kind);
      if (report.kind === 'ready') expect(report.facts).toEqual(link.helper.host.machine);
    }
  });

  test('a path’s inspection is the helper’s own', async () => {
    const link = new FakeLink();
    link.helper.host.checkouts.set('/home/me/vibesys-old', {
      state: 'too_old',
      version: '0.0.9',
      min: '0.1.0',
    });
    const {machines, manager} = await subject(new Map([['ssh:chelan4', link]]));
    await using _ = manager;
    expect(await machines.inspect(CHELAN4, '/home/me/vibesys-old')).toEqual({
      state: 'too_old',
      version: '0.0.9',
      min: '0.1.0',
    });
    expect(await machines.inspect(CHELAN4, '/nowhere')).toEqual({state: 'not_found'});
  });

  test('removing a machine forgets only it; This Mac stays on the app’s own checkout', async () => {
    const {machines, settings, manager} = await subject(new Map());
    await using _ = manager;
    expect((await machines.remove(CHELAN4)).map(entry => entry.host)).toEqual([
      'local',
      'ssh:chelan3',
    ]);
    expect(Object.keys((await settings.hosts()).settings.hosts)).toEqual(['ssh:chelan3']);
    expect((await machines.remove(LOCAL)).map(entry => entry.host)).toEqual([
      'local',
      'ssh:chelan3',
    ]);
  });
});
