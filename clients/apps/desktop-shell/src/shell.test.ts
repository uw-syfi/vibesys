import {expect, test} from 'bun:test';
import type {PreferencesView, WelcomeBridge, WelcomeEvent} from '@vibesys/ipc';
import type {ThemeMode, ThemeName} from '@vibesys/ui/tokens';
import {createShellStores} from './shell.js';

const ignore = (): void => undefined;

const STORED: PreferencesView = {
  appearance: {mode: 'system', light: 'mist', dark: 'midnight'},
  notifications: {enabled: true},
  archived: [],
};

/** The bridge calls the shell's stores make, answering from memory. */
function fakeBridge(): WelcomeBridge {
  const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});
  const ignore = (_listener: (event: WelcomeEvent<never>) => void): void => undefined;
  const bridge: Partial<WelcomeBridge> = {
    overview: () =>
      ok({
        eventCursor: 0,
        settingsProblem: null,
        recent: [],
        listings: [],
        chrome: {mode: 'welcome', attached: null, stops: {}},
        host: 'local',
      }),
    preferences: () => ok(STORED),
    setAppearance: appearance => ok({...STORED, appearance}),
    setNotifications: enabled => ok({...STORED, notifications: {enabled}}),
    archive: () => ok(STORED),
    machines: () => ok([]),
    onListing: ignore,
    onRecent: ignore,
    onPreferences: ignore,
    describeMachine: () => ok({kind: 'unsupported' as const}),
    harnesses: () => ok({kind: 'unsupported' as const}),
    examples: () => ok({kind: 'unsupported' as const}),
  };
  return bridge as WelcomeBridge;
}

test('the page follows the stored theme for the system mode, and each change of it', async () => {
  const painted: ThemeName[] = [];
  let systemListener: (mode: ThemeMode) => void = () => undefined;
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'dark', onChange: listener => (systemListener = listener)},
    {setAttribute: (_name, value) => painted.push(value)},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      session: () => {
        throw new Error('this test opens no run');
      },
    },
  );
  await Promise.resolve();
  await Promise.resolve();
  expect(painted.at(-1)).toBe('midnight');
  systemListener('light');
  expect(painted.at(-1)).toBe('mist');
  stores.settings.dispatch({
    type: 'change',
    change: {kind: 'appearance', appearance: {mode: 'dark', light: 'mist', dark: 'terminal'}},
  });
  expect(painted.at(-1)).toBe('terminal');
});
