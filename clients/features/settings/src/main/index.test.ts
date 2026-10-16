import {describe, expect, test} from 'bun:test';
import type {PreferencesView} from '@vibesys/ipc';
import {SettingsStore} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {type NativeTheme, PreferencesMain} from './index.js';

function setup(dark: boolean) {
  const store = new SettingsStore({
    settingsPath: '/s/hosts.json',
    recentPath: '/s/recent.json',
    preferencesPath: '/s/preferences.json',
    log: () => undefined,
    filesystem: new FakeSettingsFilesystem(),
  });
  const nativeTheme: NativeTheme = {themeSource: 'system', shouldUseDarkColors: dark};
  const pushed: PreferencesView[] = [];
  const main = new PreferencesMain({store, nativeTheme, push: value => pushed.push(value)});
  return {main, nativeTheme, pushed};
}

describe('PreferencesMain', () => {
  test('paints the default theme of the system mode until the user picks one', async () => {
    for (const dark of [false, true]) {
      const {main} = setup(dark);
      await main.load();
      expect(main.theme()).toBe(dark ? 'ink' : 'paper');
    }
  });

  test('drives the native theme from the mode, stores each change and pushes it', async () => {
    const {main, nativeTheme, pushed} = setup(true);
    await main.load();
    const stored = await main.answer({
      kind: 'set-appearance',
      appearance: {mode: 'light', light: 'sand', dark: 'midnight'},
    });
    expect(nativeTheme.themeSource).toBe('light');
    expect(stored.appearance.light).toBe('sand');
    expect(pushed.at(-1)).toEqual(stored);
    await main.answer({kind: 'set-notifications', enabled: false});
    expect(main.notificationsEnabled()).toBe(false);
    const archived = await main.answer({
      kind: 'archive',
      run: {host: 'local', runId: '20261015-163047'},
      archived: true,
    });
    expect(archived.archived).toEqual([{host: 'local', runId: '20261015-163047'}]);
    expect(await main.answer({kind: 'preferences'})).toEqual(archived);
  });
});
