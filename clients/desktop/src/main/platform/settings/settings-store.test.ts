import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Seeded} from '@vibesys/store/testing';
import {EMPTY_SETTINGS, withHostSettings} from './host-settings.js';
import {EMPTY_RECENT, type RecentFile, remember} from './recent.js';
import {SettingsStore} from './settings-store.js';
import {FakeSettingsFilesystem, fileError} from './testing/fake-settings-filesystem.js';

function store(
  filesystem?: FakeSettingsFilesystem,
  directory = mkdtempSync(join(tmpdir(), 'vsd-settings-')),
) {
  const logged: string[] = [];
  const settings = new SettingsStore({
    settingsPath: join(directory, 'hosts.json'),
    recentPath: join(directory, 'recent.json'),
    log: message => logged.push(message),
    ...(filesystem === undefined ? {} : {filesystem}),
  });
  return {settings, logged, directory};
}

function run(index: number) {
  return {
    host: 'local',
    project: '/p',
    task: null,
    runId: `run-${index}`,
    attachedAt: index,
  };
}

describe('SettingsStore', () => {
  test('regression: invalid host settings stay intact and block updates until corrected', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const invalidFiles = [
      '{"version":2,"hosts":{"ssh:existing":{"checkout":"/existing"}}',
      JSON.stringify({version: 2, hosts: {'ssh:existing': {checkout: 42}}}),
    ];

    for (const invalid of invalidFiles) {
      const filesystem = new FakeSettingsFilesystem({[path]: invalid});
      const {settings, logged} = store(filesystem, directory);
      expect(await settings.hosts()).toEqual(EMPTY_SETTINGS);
      expect(settings.hostsProblem).toContain(path);

      let updates = 0;
      const changing = settings.updateHosts(current => {
        updates += 1;
        return withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'});
      });
      await expect(changing).rejects.toThrow(path);
      expect(updates).toBe(0);
      expect(filesystem.text(path)).toBe(invalid);
      expect(filesystem.operations.map(operation => operation.kind)).toEqual(['read', 'read']);
      expect(logged).toHaveLength(2);

      await filesystem.writePrivateText(
        path,
        `${JSON.stringify({
          version: 2,
          hosts: {'ssh:existing': {checkout: '/existing'}},
        })}\n`,
      );
      await settings.updateHosts(current =>
        withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'}),
      );
      expect(JSON.parse(filesystem.text(path))).toEqual({
        version: 2,
        hosts: {
          'ssh:existing': {checkout: '/existing'},
          'ssh:new': {checkout: '/new'},
        },
      });
      expect(settings.hostsProblem).toBeNull();
    }
  });

  test('regression: invalid recent runs stay intact and emit nothing until corrected', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const invalidFiles = [
      '{"version":2,"runs":[{"host":"local","project":"/old"}]',
      JSON.stringify({version: 2, runs: [{...run(1), runId: null}]}),
    ];

    for (const invalid of invalidFiles) {
      const filesystem = new FakeSettingsFilesystem({[path]: invalid});
      const {settings, logged} = store(filesystem, directory);
      const heard: RecentFile[] = [];
      settings.onRecentChanged(recent => heard.push(recent));
      expect(await settings.recent()).toEqual(EMPTY_RECENT);

      let updates = 0;
      const changing = settings.updateRecent(current => {
        updates += 1;
        return remember(current, run(2));
      });
      await expect(changing).rejects.toThrow(path);
      expect(updates).toBe(0);
      expect(heard).toEqual([]);
      expect(filesystem.text(path)).toBe(invalid);
      expect(filesystem.operations.map(operation => operation.kind)).toEqual(['read', 'read']);
      expect(logged).toHaveLength(2);

      await filesystem.writePrivateText(path, `${JSON.stringify({version: 2, runs: [run(1)]})}\n`);
      await settings.updateRecent(current => remember(current, run(2)));
      expect(JSON.parse(filesystem.text(path))).toEqual({version: 2, runs: [run(2), run(1)]});
      expect(heard).toEqual([{version: 2, runs: [run(2), run(1)]}]);
    }
  });

  test('updates still initialize absent files and merge subsequent valid updates', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const {settings} = store(filesystem, directory);

    await settings.updateHosts(current =>
      withHostSettings(current, {kind: 'ssh', alias: 'one'}, {checkout: '/one'}),
    );
    await settings.updateHosts(current =>
      withHostSettings(current, {kind: 'ssh', alias: 'two'}, {checkout: '/two'}),
    );
    await settings.updateRecent(current => remember(current, run(1)));
    await settings.updateRecent(current => remember(current, run(2)));

    expect(await settings.hosts()).toEqual({
      version: 2,
      hosts: {'ssh:one': {checkout: '/one'}, 'ssh:two': {checkout: '/two'}},
    });
    expect(await settings.recent()).toEqual({version: 2, runs: [run(2), run(1)]});
  });

  test('regression: a host-settings read failure cannot replace the existing file', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const filesystem = new FakeSettingsFilesystem({
      [path]: `${JSON.stringify({
        version: 2,
        hosts: {'ssh:existing': {checkout: '/existing'}},
      })}\n`,
    });
    const {settings, logged} = store(filesystem, directory);
    filesystem.failNextRead(path, fileError('EACCES', path));
    let failure: unknown;
    try {
      await settings.updateHosts(current =>
        withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'}),
      );
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toContain(`could not read ${path}`);
    expect(settings.hostsProblem).toContain(`could not read ${path}`);
    expect(logged).toContainEqual(expect.stringContaining(`could not read ${path}`));
    expect(filesystem.operations).toEqual([{kind: 'read', path}]);
    expect(JSON.parse(filesystem.text(path))).toEqual({
      version: 2,
      hosts: {'ssh:existing': {checkout: '/existing'}},
    });

    await settings.updateHosts(current =>
      withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'}),
    );
    expect(JSON.parse(filesystem.text(path))).toEqual({
      version: 2,
      hosts: {
        'ssh:existing': {checkout: '/existing'},
        'ssh:new': {checkout: '/new'},
      },
    });
    expect(settings.hostsProblem).toBeNull();
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'read',
      'write',
      'replace',
    ]);
  });

  test('regression: a recent-runs read failure cannot replace the existing file', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const existing = {version: 2, runs: [run(1)]};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings, logged} = store(filesystem, directory);
    filesystem.failNextRead(path, fileError('EIO', path));
    let failure: unknown;
    try {
      await settings.updateRecent(current => remember(current, run(2)));
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toContain(`could not read ${path}`);
    expect(logged).toContainEqual(expect.stringContaining(`could not read ${path}`));
    expect(filesystem.operations).toEqual([{kind: 'read', path}]);
    expect(JSON.parse(filesystem.text(path))).toEqual(existing);

    await settings.updateRecent(current => remember(current, run(2)));
    expect(JSON.parse(filesystem.text(path))).toEqual({version: 2, runs: [run(2), run(1)]});
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'read',
      'write',
      'replace',
    ]);
  });

  test('regression: every recent update is pushed, so Recent never needs a manual refresh', async () => {
    const {settings} = store();
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));
    await settings.updateRecent(current => remember(current, run(1)));
    await settings.updateRecent(current => remember(current, run(2)));
    expect(heard.map(recent => recent.runs.length)).toEqual([1, 2]);
    expect(heard.at(-1)).toEqual(await settings.recent());
  });

  test('a superseded recent update is discarded at the atomic commit boundary', async () => {
    const {settings, directory} = store();
    const heard: RecentFile[] = [];
    let current = true;
    settings.onRecentChanged(recent => heard.push(recent));

    await settings.updateRecent(
      recent => {
        current = false;
        return remember(recent, run(1));
      },
      () => current,
    );

    expect(await settings.recent()).toEqual(EMPTY_RECENT);
    expect(heard).toEqual([]);
    expect(readdirSync(directory).filter(name => name.endsWith('.tmp'))).toEqual([]);
  });

  test('a cancelled host update is discarded at the atomic commit boundary', async () => {
    const {settings, directory} = store();
    let current = true;

    const outcome = await settings.updateHosts(
      hosts => {
        current = false;
        return withHostSettings(hosts, {kind: 'local'}, {checkout: '/cancelled'});
      },
      () => current,
    );

    expect(outcome).toBe('cancelled');
    expect(await settings.hosts()).toEqual(EMPTY_SETTINGS);
    expect(readdirSync(directory).filter(name => name.endsWith('.tmp'))).toEqual([]);
  });

  test('interleaved updates of both files keep every change and leave no partial file', async () => {
    const random = new Seeded(9);
    for (let round = 0; round < 10; round += 1) {
      const {settings, logged, directory} = store();
      const updates: Promise<unknown>[] = [];
      const runs: number[] = [];
      const hosts: string[] = [];
      for (let index = 1; index <= 8; index += 1) {
        if (random.bool()) {
          runs.push(index);
          updates.push(settings.updateRecent(current => remember(current, run(index))));
        } else {
          const alias = `host-${index}`;
          hosts.push(alias);
          const id = {kind: 'ssh', alias} as const;
          updates.push(
            settings.updateHosts(current =>
              withHostSettings(current, id, {checkout: `/c${index}`}),
            ),
          );
        }
        if (random.bool()) updates.push(settings.recent().then(() => undefined));
      }
      await Promise.all(updates);
      const recent = (await settings.recent()).runs.map(entry => entry.attachedAt).sort();
      expect(recent).toEqual([...runs].sort());
      expect(Object.keys((await settings.hosts()).hosts).sort()).toEqual(
        hosts.map(alias => `ssh:${alias}`).sort(),
      );
      expect(logged.filter(line => line.startsWith('ignoring'))).toEqual([]);
      expect(readdirSync(directory).filter(name => name.endsWith('.tmp'))).toEqual([]);
    }
  });
});
