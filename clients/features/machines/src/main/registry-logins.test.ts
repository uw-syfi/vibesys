import {expect, test} from 'bun:test';
import {ConnectionManager, registryRunCatalog} from '@vibesys/connection';
import {FakeLink, FakeReadCatalog} from '@vibesys/connection/testing';
import {SettingsStore, withoutHost} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {MachinesMain} from './index.js';

const HOST = {kind: 'local'} as const;

function subject(peer: FakeReadCatalog, catalog = true) {
  const link = new FakeLink();
  const manager = new ConnectionManager({
    linkFor: () => link,
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
  const main = new MachinesMain({
    settings,
    removeHost: id => settings.updateHosts(current => withoutHost(current, id)).then(() => {}),
    checkout: () => peer.checkout(),
    ...(catalog ? {catalog: registryRunCatalog(peer)} : {}),
  });
  return {main, manager, link};
}

test('regression: v2 login status uses the gateway without acquiring the helper', async () => {
  const peer = new FakeReadCatalog();
  const {main, manager, link} = subject(peer);
  await using _ = manager;
  expect(await main.agentLogins(HOST)).toEqual({kind: 'offered', value: []});
  expect(peer.requests.map(request => request.method.case)).toEqual(['listAgentLogins']);
  expect(link.helper.requests).toEqual([]);
});

test('login status failure and missing wiring never fall back to the helper', async () => {
  for (const configured of [true, false]) {
    const peer = new FakeReadCatalog();
    peer.failure = new Error('gateway denied');
    const {main, manager, link} = subject(peer, configured);
    await using _ = manager;
    await expect(main.agentLogins(HOST)).rejects.toThrow(
      configured ? 'gateway denied' : 'login catalog is missing',
    );
    expect(link.helper.requests).toEqual([]);
  }
});
