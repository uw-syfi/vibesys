import {expect, test} from 'bun:test';
import {remember, SettingsStore} from './index.js';
import {FakeSettingsFilesystem} from './testing/index.js';

function run(index: number) {
  return {
    host: 'local',
    project: '/project',
    task: null,
    runId: `run-${index}`,
    instanceId: index.toString(16).padStart(12, '0'),
    attachedAt: index,
  };
}

test('synchronous listener and diagnostic failures cannot change a durable recent update', async () => {
  const filesystem = new FakeSettingsFilesystem();
  const logged: string[] = [];
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem,
    log: message => {
      logged.push(message);
      throw new Error('synchronous logger failure');
    },
  });
  const heard: number[] = [];
  settings.onRecentChanged(() => {
    throw new Error('synchronous listener failure');
  });
  settings.onRecentChanged(recent => {
    heard.push(recent.runs.length);
  });

  await settings.updateRecent(current => remember(current, run(1)));

  expect(JSON.parse(filesystem.text('/settings/recent.json'))).toEqual({
    version: 4,
    runs: [run(1)],
  });
  expect(heard).toEqual([1]);
  expect(logged).toEqual(['could not notify a recent-runs listener: synchronous listener failure']);
});

test('rejected listener and diagnostic promises are terminally contained after commit', async () => {
  const filesystem = new FakeSettingsFilesystem();
  const logged: string[] = [];
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem,
    log: message => {
      logged.push(message);
      return Promise.reject(new Error('asynchronous logger failure'));
    },
  });
  const heard: number[] = [];
  settings.onRecentChanged(() => Promise.reject(new Error('asynchronous listener failure')));
  settings.onRecentChanged(recent => {
    heard.push(recent.runs.length);
  });

  await settings.updateRecent(current => remember(current, run(1)));
  await Promise.resolve();

  expect(JSON.parse(filesystem.text('/settings/recent.json'))).toEqual({
    version: 4,
    runs: [run(1)],
  });
  expect(heard).toEqual([1]);
  expect(logged).toEqual([
    'could not notify a recent-runs listener: asynchronous listener failure',
  ]);

  await settings.updateRecent(current => remember(current, run(2)));
  await Promise.resolve();
  expect(heard).toEqual([1, 2]);
  expect((await settings.recent()).runs).toEqual([run(2), run(1)]);
});

test('recent delivery snapshots registration for the current event', async () => {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  const heard: string[] = [];
  let firstDelivery = true;
  let unsubscribeSelf = (): void => {};
  let unsubscribeLater = (): void => {};
  const self = (recent: {readonly runs: readonly unknown[]}): void => {
    heard.push(`self:${recent.runs.length}`);
    if (!firstDelivery) return;
    firstDelivery = false;
    unsubscribeSelf();
    unsubscribeSelf = settings.onRecentChanged(self);
    unsubscribeLater();
  };
  unsubscribeSelf = settings.onRecentChanged(self);
  unsubscribeLater = settings.onRecentChanged(recent => {
    heard.push(`later:${recent.runs.length}`);
  });

  await settings.updateRecent(current => remember(current, run(1)));
  await settings.updateRecent(current => remember(current, run(2)));

  expect(heard).toEqual(['self:1', 'later:1', 'self:2']);
});

test('host settings notify every window only after a committed atomic write', async () => {
  const filesystem = new FakeSettingsFilesystem();
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem,
    log: () => {},
  });
  const first: boolean[] = [];
  const second: boolean[] = [];
  const stopFirst = settings.onHostsChanged(file => {
    expect(JSON.parse(filesystem.text('/settings/hosts.json'))).toEqual(file);
    first.push(file.hosts['local']?.runTransportV2 ?? false);
  });
  const stopSecond = settings.onHostsChanged(file =>
    second.push(file.hosts['local']?.runTransportV2 ?? false),
  );
  for (const enabled of [true, false, true]) {
    const next = {
      version: 2 as const,
      hosts: {local: {checkout: '/src/vibesys', runTransportV2: enabled}},
    };
    expect(
      await settings.updateHosts(
        () => next,
        () => false,
      ),
    ).toBe('cancelled');
    expect(await settings.updateHosts(() => next)).toBe('committed');
  }
  stopFirst();
  stopSecond();
  await settings.updateHosts(() => ({version: 2, hosts: {}}));
  expect(first).toEqual([true, false, true]);
  expect(second).toEqual(first);
});
