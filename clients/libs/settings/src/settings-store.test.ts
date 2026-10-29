import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readdirSync} from 'node:fs';
import {lstat, mkdtemp, readFile, rm, symlink, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {isDeepStrictEqual} from 'node:util';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {EMPTY_SETTINGS, withHostSettings} from './host-settings.js';
import {decodePreferences, decodeRecent, decodeSettings, settingsBinaryPath} from './index.js';
import {EMPTY_RECENT, type RecentFile, recentDocument, remember} from './recent.js';
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
    preferencesPath: join(directory, 'preferences.json'),
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
    instanceId: index.toString(16).padStart(12, '0'),
    attachedAt: index,
  };
}

function version2Run(index: number) {
  return {
    host: 'local',
    project: '/p',
    task: null,
    runId: `run-${index}`,
    attachedAt: index,
  };
}

function migratedVersion2Run(index: number) {
  return {...version2Run(index), instanceId: null};
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
      const observed = await settings.hosts();
      expect(observed.settings).toEqual(EMPTY_SETTINGS);
      expect(observed.problem).toContain(path);

      let updates = 0;
      const changing = settings.updateHosts(current => {
        updates += 1;
        return withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'});
      });
      await expect(changing).rejects.toThrow(path);
      expect(updates).toBe(0);
      expect(filesystem.text(path)).toBe(invalid);
      expect(filesystem.operations.map(operation => operation.kind)).toEqual([
        'read',
        'read',
        'read',
        'read',
        'read',
      ]);
      expect(logged).toHaveLength(2);

      filesystem.setText(
        path,
        `${JSON.stringify({
          version: 2,
          hosts: {'ssh:existing': {checkout: '/existing'}},
        })}\n`,
      );
      await settings.updateHosts(current =>
        withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'}),
      );
      expect(readWritten(filesystem, path)).toEqual({
        version: 2,
        hosts: {
          'ssh:existing': {checkout: '/existing'},
          'ssh:new': {checkout: '/new'},
        },
      });
      expect((await settings.hosts()).problem).toBeNull();
    }
  });

  test('regression: invalid recent runs stay intact and emit nothing until corrected', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const invalidFiles = ['{"version":2,"runs":[{"host":"local","project":"/old"}]'];

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
      expect(filesystem.operations.map(operation => operation.kind)).toEqual([
        'read',
        'read',
        'read',
        'read',
        'read',
      ]);
      expect(logged).toHaveLength(2);

      filesystem.setText(path, `${JSON.stringify({version: 2, runs: [version2Run(1)]})}\n`);
      await settings.updateRecent(current => remember(current, run(2)));
      const expected = {version: 4 as const, runs: [run(2), migratedVersion2Run(1)]};
      expect(readWritten(filesystem, path)).toEqual(expected);
      expect(heard).toEqual([expected]);
    }
  });

  test('a recent file with invalid entries lists the valid ones and reports the rest', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const text = `${JSON.stringify({
      version: 4,
      runs: [run(3), {...run(2), task: ''}, run(1), {...run(0), instanceId: 'bad'}],
    })}\n`;
    const filesystem = new FakeSettingsFilesystem({[path]: text});
    const {settings, logged} = store(filesystem, directory);

    const snapshot = await settings.recentSnapshot();

    expect(snapshot.recent).toEqual({version: 4, runs: [run(3), run(1)]});
    expect(snapshot.skipped.map(each => each.index)).toEqual([1, 3]);
    expect(snapshot.skipped[0]?.reason).toBe('recent.json: runs[1].task must be a task name');
    expect(logged.join('\n')).toContain('runs[3].instanceId');
    expect(filesystem.text(path)).toBe(text);
  });

  test('regression: recording a run keeps every invalid entry as written and every valid one', async () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const directory = '/settings';
      const path = join(directory, 'recent.json');
      const entries: unknown[] = [];
      const invalid: unknown[] = [];
      for (let index = 0; index < random.int(8); index++) {
        if (random.int(2) === 0) {
          const bad = random.pick([
            {...run(index), task: ''},
            {...run(index), instanceId: 'bad'},
            {...run(index), extra: [index, {nested: null}]},
            [index, 'not an object'],
            null,
          ]);
          invalid.push(bad);
          entries.push(bad);
        } else {
          entries.push(run(index));
        }
      }
      const filesystem = new FakeSettingsFilesystem({
        [path]: JSON.stringify({version: 4, runs: entries}),
      });
      const {settings} = store(filesystem, directory);
      const before = await settings.recentSnapshot();

      await settings.updateRecent(current => remember(current, run(100)));

      const written = readWritten(filesystem, path) as {runs: unknown[]};
      const validWritten = written.runs.filter(
        entry => !invalid.some(bad => isDeepStrictEqual(bad, entry)),
      );
      expect(
        written.runs.filter(entry => invalid.some(bad => isDeepStrictEqual(bad, entry))),
      ).toEqual(invalid);
      expect<unknown[]>(validWritten).toEqual([run(100), ...before.recent.runs].slice(0, 20));
      const after = await settings.recentSnapshot();
      expect([...after.recent.runs] as unknown[]).toEqual(validWritten);
      expect(after.skipped.map(each => each.reason.replace(/runs\[\d+\]/, 'runs[]'))).toEqual(
        before.skipped.map(each => each.reason.replace(/runs\[\d+\]/, 'runs[]')),
      );
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

    expect((await settings.hosts()).settings).toEqual({
      version: 2,
      hosts: {'ssh:one': {checkout: '/one'}, 'ssh:two': {checkout: '/two'}},
    });
    expect(await settings.recent()).toEqual({version: 4, runs: [run(2), run(1)]});
  });

  test('directory-synced host and recent updates survive a crash and a fresh store', async () => {
    const directory = '/settings';
    const hostsPath = join(directory, 'hosts.json');
    const recentPath = join(directory, 'recent.json');
    const filesystem = new FakeSettingsFilesystem();
    const {settings} = store(filesystem, directory);
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));

    await expect(
      settings.updateHosts(current =>
        withHostSettings(current, {kind: 'local'}, {checkout: '/project'}),
      ),
    ).resolves.toBe('committed');
    await settings.updateRecent(current => remember(current, run(1)));
    expect(heard).toHaveLength(1);

    filesystem.crash();
    const recovered = store(filesystem, directory).settings;
    expect((await recovered.hosts()).settings.hosts['local']).toEqual({checkout: '/project'});
    expect(await recovered.recent()).toEqual({version: 4, runs: [run(1)]});
    expect(filesystem.operations).toContainEqual({
      kind: 'sync-file',
      path: `${settingsBinaryPath(hostsPath)}.0.tmp`,
    });
    expect(filesystem.operations).toContainEqual({kind: 'sync-directory', path: directory});
    expect(filesystem.operations).toContainEqual({
      kind: 'sync-file',
      path: `${settingsBinaryPath(recentPath)}.1.tmp`,
    });
  });

  test('an unavailable directory sync acknowledges visibility without claiming crash durability', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem({}, {directorySync: 'unavailable'});
    const {settings} = store(filesystem, directory);
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));
    expect(() => filesystem.failNextDirectorySync('/settings/recent.json', new Error())).toThrow(
      'capability is unavailable',
    );

    await settings.updateRecent(current => remember(current, run(1)));
    expect(await settings.recent()).toEqual({version: 4, runs: [run(1)]});
    expect(heard).toEqual([{version: 4, runs: [run(1)]}]);
    expect(filesystem.operations).toContainEqual({
      kind: 'directory-sync-unavailable',
      path: directory,
    });
    expect(
      filesystem.operations.some(operation => operation.kind === 'sync-directory'),
    ).toBeFalse();

    filesystem.crash();
    expect(await store(filesystem, directory).settings.recent()).toEqual(EMPTY_RECENT);
  });

  test('every directory-synced update in a generated sequence survives immediate recovery', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const expectedHosts: string[] = [];
    const expectedRuns: number[] = [];
    const random = new Seeded(2820);
    let settings = store(filesystem, directory).settings;

    for (let index = 1; index <= 20; index += 1) {
      if (random.bool()) {
        const alias = `host-${index}`;
        expectedHosts.push(`ssh:${alias}`);
        await expect(
          settings.updateHosts(current =>
            withHostSettings(current, {kind: 'ssh', alias}, {checkout: `/checkout/${index}`}),
          ),
        ).resolves.toBe('committed');
      } else {
        expectedRuns.push(index);
        await settings.updateRecent(current => remember(current, run(index)));
      }

      filesystem.crash();
      settings = store(filesystem, directory).settings;
      expect(Object.keys((await settings.hosts()).settings.hosts).sort()).toEqual(
        [...expectedHosts].sort(),
      );
      expect((await settings.recent()).runs.map(entry => entry.attachedAt).sort()).toEqual(
        [...expectedRuns].sort(),
      );
    }
  });

  test('a staged-file sync failure reports no host commit and recovers the old file', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const existing = {version: 2 as const, hosts: {local: {checkout: '/old'}}};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings} = store(filesystem, directory);
    await settings.hosts();
    filesystem.operations.length = 0;
    filesystem.failNextFileSync(settingsBinaryPath(path), fileError('EIO', path));

    const changing = settings.updateHosts(current =>
      withHostSettings(current, {kind: 'local'}, {checkout: '/new'}),
    );
    await expect(changing).rejects.toThrow('EIO');
    expect(readWritten(filesystem, path)).toEqual(existing);

    filesystem.crash();
    expect((await store(filesystem, directory).settings.hosts()).settings).toEqual(existing);
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'write',
      'sync-file',
      'crash',
      'read',
    ]);
  });

  test('a staged-file sync failure rejects a recent update without announcing it', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const existing = {version: 4 as const, runs: [run(1)]};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings} = store(filesystem, directory);
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));
    await settings.recent();
    filesystem.operations.length = 0;
    filesystem.failNextFileSync(settingsBinaryPath(path), fileError('EIO', path));

    await expect(settings.updateRecent(current => remember(current, run(2)))).rejects.toThrow(
      'EIO',
    );
    expect(heard).toEqual([]);
    filesystem.crash();
    expect(await store(filesystem, directory).settings.recent()).toEqual(existing);
  });

  test('a directory sync failure preserves the ambiguous post-rename host state', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const existing = {version: 2 as const, hosts: {local: {checkout: '/old'}}};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings} = store(filesystem, directory);
    await settings.hosts();
    filesystem.operations.length = 0;
    filesystem.failNextDirectorySync(settingsBinaryPath(path), fileError('EIO', directory));

    const changing = settings.updateHosts(current =>
      withHostSettings(current, {kind: 'local'}, {checkout: '/new'}),
    );
    await expect(changing).rejects.toThrow('EIO');

    // Rename succeeded before the failed directory sync. Reporting failure does not promise that
    // the replacement was rolled back: it is visible now, but its crash outcome is unknown.
    expect((await settings.hosts()).settings.hosts['local']).toEqual({checkout: '/new'});
    filesystem.crash();
    expect((await store(filesystem, directory).settings.hosts()).settings).toEqual(existing);
  });

  test('a directory sync failure does not announce an ambiguously replaced recent file', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const existing = {version: 4 as const, runs: [run(1)]};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings} = store(filesystem, directory);
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));
    await settings.recent();
    filesystem.operations.length = 0;
    filesystem.failNextDirectorySync(settingsBinaryPath(path), fileError('EIO', directory));

    await expect(settings.updateRecent(current => remember(current, run(2)))).rejects.toThrow(
      'EIO',
    );
    expect(heard).toEqual([]);
    expect((await settings.recent()).runs).toEqual([run(2), run(1)]);

    filesystem.crash();
    expect(await store(filesystem, directory).settings.recent()).toEqual(existing);
  });

  test('staging chooses a fresh sibling without overwriting an existing entry', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const occupied = `${settingsBinaryPath(path)}.0.tmp`;
    const filesystem = new FakeSettingsFilesystem({[occupied]: 'owned elsewhere'});
    const {settings} = store(filesystem, directory);

    await settings.updateHosts(current =>
      withHostSettings(current, {kind: 'local'}, {checkout: '/project'}),
    );

    expect(filesystem.text(occupied)).toBe('owned elsewhere');
    expect(filesystem.operations).toContainEqual({
      kind: 'write',
      path: `${settingsBinaryPath(path)}.1.tmp`,
    });
    expect((await settings.hosts()).settings.hosts['local']).toEqual({checkout: '/project'});
  });

  test('both settings writers leave planted staging symlinks and their targets untouched', async () => {
    for (const targetName of ['hosts.json', 'recent.json'] as const) {
      const directory = await mkdtemp(join(tmpdir(), 'vsd-settings-link-'));
      const path = join(directory, targetName);
      const unrelated = join(directory, 'unrelated');
      const planted = `${settingsBinaryPath(path)}.${process.pid}.tmp`;
      try {
        await writeFile(unrelated, 'keep me\n', {mode: 0o644});
        const unrelatedMode = (await lstat(unrelated)).mode & 0o777;
        await symlink(unrelated, planted);
        const {settings} = store(undefined, directory);

        if (targetName === 'hosts.json') {
          await settings.updateHosts(current =>
            withHostSettings(current, {kind: 'local'}, {checkout: '/project'}),
          );
        } else {
          await settings.updateRecent(current => remember(current, run(1)));
        }

        const installed = await lstat(settingsBinaryPath(path));
        expect(installed.isFile()).toBeTrue();
        expect(installed.isSymbolicLink()).toBeFalse();
        expect(installed.mode & 0o777).toBe(0o600);
        const bytes = await readFile(settingsBinaryPath(path));
        const written =
          targetName === 'hosts.json' ? decodeSettings(bytes) : decodeRecent(bytes).file;
        expect(written.version).toBe(targetName === 'hosts.json' ? 2 : 4);
        expect((await lstat(planted)).isSymbolicLink()).toBeTrue();
        expect(await readFile(unrelated, 'utf8')).toBe('keep me\n');
        expect((await lstat(unrelated)).mode & 0o777).toBe(unrelatedMode);
      } finally {
        await rm(directory, {recursive: true, force: true});
      }
    }
  });

  test('a regular settings file is durably replaced with exact private permissions', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'vsd-settings-file-'));
    const path = join(directory, 'hosts.json');
    try {
      await writeFile(path, `${JSON.stringify(EMPTY_SETTINGS)}\n`, {mode: 0o644});
      const {settings} = store(undefined, directory);

      await settings.updateHosts(current =>
        withHostSettings(current, {kind: 'local'}, {checkout: '/project'}),
      );

      const installed = await lstat(settingsBinaryPath(path));
      expect(installed.isFile()).toBeTrue();
      expect(installed.mode & 0o777).toBe(0o600);
      expect((await settings.hosts()).settings.hosts['local']).toEqual({checkout: '/project'});
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
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
    expect(logged).toContainEqual(expect.stringContaining(`could not read ${path}`));
    expect(filesystem.operations).toEqual([
      {kind: 'read', path: settingsBinaryPath(path)},
      {kind: 'read', path},
    ]);
    expect(JSON.parse(filesystem.text(path))).toEqual({
      version: 2,
      hosts: {'ssh:existing': {checkout: '/existing'}},
    });

    await settings.updateHosts(current =>
      withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'}),
    );
    expect(readWritten(filesystem, path)).toEqual({
      version: 2,
      hosts: {
        'ssh:existing': {checkout: '/existing'},
        'ssh:new': {checkout: '/new'},
      },
    });
    expect((await settings.hosts()).problem).toBeNull();
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'read',
      'read',
      'read',
      'write',
      'sync-file',
      'replace',
      'sync-directory',
      'write',
      'sync-file',
      'replace',
      'sync-directory',
      'read',
    ]);
  });

  test('regression: a recent-runs read failure cannot replace the existing file', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const existing = {version: 2, runs: [version2Run(1)]};
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
    expect(filesystem.operations).toEqual([
      {kind: 'read', path: settingsBinaryPath(path)},
      {kind: 'read', path},
    ]);
    expect(JSON.parse(filesystem.text(path))).toEqual(existing);

    await settings.updateRecent(current => remember(current, run(2)));
    expect(readWritten(filesystem, path)).toEqual({
      version: 4,
      runs: [run(2), migratedVersion2Run(1)],
    });
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'read',
      'read',
      'read',
      'write',
      'sync-file',
      'replace',
      'sync-directory',
      'write',
      'sync-file',
      'replace',
      'sync-directory',
    ]);
  });

  test('concurrent host reads keep each diagnostic with the value that produced it', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const valid = {
      version: 2 as const,
      hosts: {'ssh:existing': {checkout: '/existing'}},
    };
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(valid)}\n`});
    const {settings} = store(filesystem, directory);
    filesystem.failNextRead(path, fileError('EACCES', path));

    const failed = settings.hosts();
    const succeeded = settings.hosts();

    await expect(failed).resolves.toEqual({
      settings: EMPTY_SETTINGS,
      problem: expect.stringContaining(`could not read ${path}`),
    });
    await expect(succeeded).resolves.toEqual({settings: valid, problem: null});
  });

  test('regression: a failed host-settings replace removes its staged file', async () => {
    const directory = '/settings';
    const path = join(directory, 'hosts.json');
    const existing = {version: 2, hosts: {'ssh:existing': {checkout: '/existing'}}};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings} = store(filesystem, directory);
    await settings.hosts();
    filesystem.operations.length = 0;
    filesystem.failNextReplace(settingsBinaryPath(path), fileError('ENOSPC', path));

    const changing = settings.updateHosts(current =>
      withHostSettings(current, {kind: 'ssh', alias: 'new'}, {checkout: '/new'}),
    );
    await expect(changing).rejects.toThrow('ENOSPC');

    expect(readWritten(filesystem, path)).toEqual(existing);
    const staging = filesystem.operations.find(operation => operation.kind === 'write')?.path;
    expect(staging).toBeDefined();
    if (staging === undefined) throw new Error('expected a staged write');
    expect(() => filesystem.text(staging)).toThrow();
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'write',
      'sync-file',
      'replace',
      'remove',
    ]);
  });

  test('regression: a failed recent-runs replace removes its staged file', async () => {
    const directory = '/settings';
    const path = join(directory, 'recent.json');
    const existing = {version: 4 as const, runs: [run(1)]};
    const filesystem = new FakeSettingsFilesystem({[path]: `${JSON.stringify(existing)}\n`});
    const {settings} = store(filesystem, directory);
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));
    await settings.recent();
    filesystem.operations.length = 0;
    filesystem.failNextReplace(settingsBinaryPath(path), fileError('EACCES', path));

    const changing = settings.updateRecent(current => remember(current, run(2)));
    await expect(changing).rejects.toThrow('EACCES');

    expect(heard).toEqual([]);
    expect(readWritten(filesystem, path)).toEqual(existing);
    const staging = filesystem.operations.find(operation => operation.kind === 'write')?.path;
    expect(staging).toBeDefined();
    if (staging === undefined) throw new Error('expected a staged write');
    expect(() => filesystem.text(staging)).toThrow();
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'write',
      'sync-file',
      'replace',
      'remove',
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
    expect((await settings.hosts()).settings).toEqual(EMPTY_SETTINGS);
    expect(readdirSync(directory).filter(name => name.endsWith('.tmp'))).toEqual([]);
  });

  test('a commit guard failure removes the staging file it owns', async () => {
    const directory = '/settings';
    const filesystem = new FakeSettingsFilesystem();
    const {settings} = store(filesystem, directory);
    const guardFailure = new Error('guard failed');

    const changing = settings.updateHosts(
      current => withHostSettings(current, {kind: 'local'}, {checkout: '/project'}),
      () => {
        throw guardFailure;
      },
    );

    await expect(changing).rejects.toBe(guardFailure);
    const staging = filesystem.operations.find(operation => operation.kind === 'write')?.path;
    expect(staging).toBeDefined();
    if (staging === undefined) throw new Error('expected a staged write');
    expect(() => filesystem.text(staging)).toThrow();
    expect(filesystem.operations.map(operation => operation.kind)).toEqual([
      'read',
      'read',
      'write',
      'sync-file',
      'remove',
    ]);
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
      expect(Object.keys((await settings.hosts()).settings.hosts).sort()).toEqual(
        hosts.map(alias => `ssh:${alias}`).sort(),
      );
      expect(logged.filter(line => line.startsWith('ignoring'))).toEqual([]);
      expect(readdirSync(directory).filter(name => name.endsWith('.tmp'))).toEqual([]);
    }
  });
});

function readWritten(filesystem: FakeSettingsFilesystem, path: string): unknown {
  const bytes = filesystem.bytes(settingsBinaryPath(path));
  if (path.endsWith('hosts.json')) return decodeSettings(bytes);
  if (path.endsWith('preferences.json')) return decodePreferences(bytes);
  const recent = decodeRecent(bytes);
  return recentDocument(recent.file, recent.invalid);
}
