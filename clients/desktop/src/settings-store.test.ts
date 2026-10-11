import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Seeded} from '@vibesys/core-state/testing';
import {withHostSettings} from './host-settings.js';
import type {RecentFile} from './recent.js';
import {remember} from './recent.js';
import {SettingsStore} from './settings-store.js';

function store() {
  const directory = mkdtempSync(join(tmpdir(), 'vsd-settings-'));
  const logged: string[] = [];
  const settings = new SettingsStore({
    settingsPath: join(directory, 'hosts.json'),
    recentPath: join(directory, 'recent.json'),
    log: message => logged.push(message),
  });
  return {settings, logged, directory};
}

function run(index: number) {
  return {
    host: 'local',
    project: '/p',
    task: null,
    instanceId: index.toString(16).padStart(12, '0'),
    runId: null,
    attachedAt: index,
  };
}

describe('SettingsStore', () => {
  test('regression: every recent update is pushed, so Recent never needs a manual refresh', async () => {
    const {settings} = store();
    const heard: RecentFile[] = [];
    settings.onRecentChanged(recent => heard.push(recent));
    await settings.updateRecent(current => remember(current, run(1)));
    await settings.updateRecent(current => remember(current, run(2)));
    expect(heard.map(recent => recent.runs.length)).toEqual([1, 2]);
    expect(heard.at(-1)).toEqual(await settings.recent());
  });

  test('interleaved updates of both files keep every change and leave no partial file', async () => {
    const random = new Seeded(9);
    for (let round = 0; round < 10; round += 1) {
      const {settings, logged, directory} = store();
      const updates: Promise<void>[] = [];
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
