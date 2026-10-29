import {describe, expect, test} from 'bun:test';
import type {RunRef} from '@vibesys/runs';
import {
  DEFAULT_PREFERENCES,
  isArchived,
  type Preferences,
  parsePreferences,
  SettingsStore,
  withArchived,
} from './index.js';
import {FakeSettingsFilesystem} from './testing/index.js';

const RUNS: readonly RunRef[] = [
  {host: 'local', runId: '20261015-163047'},
  {host: 'ssh:gpu', runId: '20261015-163047'},
  {host: 'local', runId: '20261014-090000'},
];

/** A deterministic stream of numbers in [0, 1), so a property's cases are the same on every run. */
function seeded(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1103515245 + 12345) % 2147483648;
    return state / 2147483648;
  };
}

describe('parsePreferences', () => {
  test('reads what it writes, and the defaults are valid', () => {
    const changed: Preferences = {
      version: 1,
      appearance: {mode: 'dark', light: 'sand', dark: 'terminal'},
      notifications: {enabled: false},
      archived: RUNS,
    };
    for (const preferences of [DEFAULT_PREFERENCES, changed]) {
      expect(parsePreferences(JSON.parse(JSON.stringify(preferences)))).toEqual(preferences);
    }
  });

  test('rejects unknown keys, a theme of the wrong mode, and a bad run, naming the path', () => {
    const cases: readonly [unknown, string][] = [
      [{...DEFAULT_PREFERENCES, extra: 1}, 'preferences.extra'],
      [
        {...DEFAULT_PREFERENCES, appearance: {mode: 'system', light: 'ink', dark: 'ink'}},
        'preferences.appearance.light',
      ],
      [
        {...DEFAULT_PREFERENCES, appearance: {mode: 'auto', light: 'paper', dark: 'ink'}},
        'preferences.appearance.mode',
      ],
      [
        {...DEFAULT_PREFERENCES, archived: [{host: 'local', runId: '../x'}]},
        'preferences.archived[0].runId',
      ],
      [
        {...DEFAULT_PREFERENCES, notifications: {enabled: 'yes'}},
        'preferences.notifications.enabled',
      ],
    ];
    for (const [value, path] of cases) expect(() => parsePreferences(value)).toThrow(path);
  });
});

describe('withArchived', () => {
  test('archives each run at most once and restores it, over seeded sequences', () => {
    for (let seed = 1; seed <= 50; seed++) {
      const random = seeded(seed);
      let preferences = DEFAULT_PREFERENCES;
      const model = new Set<number>();
      for (let step = 0; step < 30; step++) {
        const index = Math.floor(random() * RUNS.length);
        const archive = random() < 0.6;
        preferences = withArchived(preferences, RUNS[index] as RunRef, archive);
        if (archive) model.add(index);
        else model.delete(index);
        expect(preferences.archived).toHaveLength(model.size);
        for (const [at, run] of RUNS.entries())
          expect(isArchived(preferences, run)).toBe(model.has(at));
      }
    }
  });
});

describe('SettingsStore preferences', () => {
  test('defaults when absent, persists updates, and announces each one', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const store = new SettingsStore({
      settingsPath: '/settings/hosts.json',
      recentPath: '/settings/recent.json',
      preferencesPath: '/settings/preferences.json',
      log: () => undefined,
      filesystem,
    });
    expect(await store.preferences()).toEqual(DEFAULT_PREFERENCES);
    const heard: Preferences[] = [];
    store.onPreferencesChanged(preferences => heard.push(preferences));
    const next = await store.updatePreferences(current => ({
      ...current,
      appearance: {...current.appearance, mode: 'light', light: 'mist'},
    }));
    expect(next.appearance).toEqual({mode: 'light', light: 'mist', dark: 'ink'});
    expect(await store.preferences()).toEqual(next);
    expect(heard).toEqual([next]);
  });
});
