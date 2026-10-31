import {expect, test} from 'bun:test';
import {ConnectionManager, registryRunCatalog} from '@vibesys/connection';
import {FakeReadCatalog, idleHostRuns, idleSubscriptions} from '@vibesys/connection/testing';
import {SettingsStore} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {Hosts} from './index.js';

const HOST = {kind: 'local'} as const;

function subject(peer: FakeReadCatalog) {
  const manager = new ConnectionManager({
    linkFor: () => {
      throw new Error('helper connection must not be acquired');
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
  const hosts = new Hosts({
    runs: idleHostRuns(),
    subscriptions: idleSubscriptions(),
    manager,
    settings,
    localCheckout: '/checkout',
    sshConfigPath: '/missing',
    log: () => {},
    push: () => {},
    retire: () => {},
    catalog: registryRunCatalog(peer),
  });
  return {hosts, manager};
}

test('regression: v2 task and harness catalogs never acquire a helper connection', async () => {
  const peer = new FakeReadCatalog();
  peer.projects.set('/project', ['queue']);
  peer.harnessOptions = [
    {
      provider: 'codex',
      model: 'model-a',
      default: true,
      display: {
        providerName: 'Codex',
        modelName: 'Model A',
        generation: {id: 'a', name: 'A', order: 1n},
      },
    },
  ];
  const {hosts, manager} = subject(peer);
  await using _held = manager;
  expect(await hosts.tasks(HOST, '/project')).toEqual(['queue']);
  expect(await hosts.harnesses(HOST)).toMatchObject({
    kind: 'listed',
    harnesses: [
      {
        provider: 'codex',
        model: 'model-a',
        default: true,
        display: {
          providerName: 'Codex',
          modelName: 'Model A',
          generation: {id: 'a', name: 'A', order: 1},
        },
      },
    ],
  });
});

test('v2 catalog failures never acquire a helper connection', async () => {
  const peer = new FakeReadCatalog();
  peer.failure = new Error('gateway denied');
  const {hosts, manager} = subject(peer);
  await using _held = manager;
  await expect(hosts.tasks(HOST, '/project')).rejects.toThrow('gateway denied');
  await expect(hosts.harnesses(HOST)).rejects.toThrow('gateway denied');
});
