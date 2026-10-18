import {expect, test} from 'bun:test';
import type {PreferencesView} from '@vibesys/ipc';
import {UNEXPECTED_FAILURE} from '@vibesys/ipc';
import {createSettingsStore, type SettingsBridge} from './store.js';

const PREFERENCES: PreferencesView = {
  appearance: {mode: 'system', light: 'paper', dark: 'ink'},
  notifications: {enabled: true},
  archived: [],
};

async function settle(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

/** A bridge that loads, and whose saves are the given call. */
function bridgeSaving(save: SettingsBridge['setNotifications']): SettingsBridge {
  return {
    preferences: () => Promise.resolve({ok: true, value: PREFERENCES}),
    setAppearance: () => Promise.resolve({ok: true, value: PREFERENCES}),
    setNotifications: save,
    archive: () => Promise.resolve({ok: true, value: PREFERENCES}),
    onPreferences: () => undefined,
  };
}

test('a save whose bridge call throws or rejects says something unexpected, not the raw error', async () => {
  const raw = 'bridge.setNotifications is not a function';
  const throwing: SettingsBridge['setNotifications'] = () => {
    throw new TypeError(raw);
  };
  const rejecting: SettingsBridge['setNotifications'] = () => Promise.reject(new Error(raw));
  for (const save of [throwing, rejecting]) {
    const store = createSettingsStore(bridgeSaving(save), {
      mode: 'light',
      onChange: () => undefined,
    });
    await settle();
    store.dispatch({type: 'change', change: {kind: 'notifications', enabled: false}});
    await settle();

    const state = store.getState();

    expect(state.kind === 'ready' ? state.problem : null).toBe(UNEXPECTED_FAILURE);
  }
});

test('a failure the main process reported keeps its words', async () => {
  const refusing: SettingsBridge['setNotifications'] = () =>
    Promise.resolve({ok: false, error: 'The settings file is read-only.', authNeeded: false});
  const store = createSettingsStore(bridgeSaving(refusing), {
    mode: 'light',
    onChange: () => undefined,
  });
  await settle();
  store.dispatch({type: 'change', change: {kind: 'notifications', enabled: false}});
  await settle();

  const state = store.getState();

  expect(state.kind === 'ready' ? state.problem : null).toBe('The settings file is read-only.');
});
