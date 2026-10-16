import {describe, expect, test} from 'bun:test';
import {lstat, mkdtemp, readdir, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {SettingsStore} from './index.js';
import {type DirectorySyncCapability, fileError, NodeSettingsFilesystem} from './testing/index.js';

function store(directory: string, directorySync: DirectorySyncCapability): SettingsStore {
  return new SettingsStore({
    settingsPath: join(directory, 'hosts.json'),
    recentPath: join(directory, 'recent.json'),
    preferencesPath: join(directory, 'preferences.json'),
    filesystem: new NodeSettingsFilesystem({directorySync}),
    log: () => {},
  });
}

describe('NodeSettingsFilesystem platform durability', () => {
  test('the Windows capability accepts file-synced atomic replacements without directory sync', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'vsd-settings-windows-'));
    const hostsPath = join(directory, 'hosts.json');
    const recentPath = join(directory, 'recent.json');
    try {
      const settings = store(directory, {kind: 'unavailable'});

      await expect(
        settings.updateHosts(current => ({
          ...current,
          hosts: {...current.hosts, local: {checkout: '/project'}},
        })),
      ).resolves.toBe('committed');
      await expect(settings.updateRecent(() => ({version: 4, runs: []}))).resolves.toBeUndefined();

      expect(JSON.parse(await readFile(hostsPath, 'utf8'))).toEqual({
        version: 2,
        hosts: {local: {checkout: '/project'}},
      });
      expect(JSON.parse(await readFile(recentPath, 'utf8'))).toEqual({version: 4, runs: []});
      expect((await lstat(hostsPath)).mode & 0o777).toBe(0o600);
      expect((await lstat(recentPath)).mode & 0o777).toBe(0o600);
      expect((await readdir(directory)).some(name => name.endsWith('.tmp'))).toBeFalse();
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });

  test('an available POSIX directory-sync failure is not swallowed after rename', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'vsd-settings-posix-'));
    const hostsPath = join(directory, 'hosts.json');
    const failure = fileError('EPERM', directory);
    const synced: string[] = [];
    try {
      const settings = store(directory, {
        kind: 'available',
        sync: async path => {
          synced.push(path);
          throw failure;
        },
      });

      await expect(
        settings.updateHosts(current => ({
          ...current,
          hosts: {...current.hosts, local: {checkout: '/visible-but-ambiguous'}},
        })),
      ).rejects.toBe(failure);

      expect(synced).toEqual([directory]);
      expect(JSON.parse(await readFile(hostsPath, 'utf8'))).toEqual({
        version: 2,
        hosts: {local: {checkout: '/visible-but-ambiguous'}},
      });
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  });
});
