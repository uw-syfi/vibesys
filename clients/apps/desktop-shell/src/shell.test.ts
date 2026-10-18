import {expect, test} from 'bun:test';
import type {Screen} from '@vibesys/feature-app-shell/state';
import type {PreferencesView, WelcomeBridge, WelcomeEvent} from '@vibesys/ipc';
import type {ThemeMode, ThemeName} from '@vibesys/ui/tokens';
import {createShellStores} from './shell.js';

const ignore = (): void => undefined;

/** A window address that holds no zoom: these tests open no run. */
const NO_ADDRESS = {
  fragment: () => '',
  push: ignore,
  onRestore: () => ignore,
  onEscape: () => ignore,
};

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
        skippedRecent: [],
        listings: [],
        chrome: {mode: 'welcome', attached: null, stops: {}},
        host: 'local',
      }),
    preferences: () => ok(STORED),
    setAppearance: appearance => ok({...STORED, appearance}),
    setNotifications: enabled => ok({...STORED, notifications: {enabled}}),
    archive: () => ok(STORED),
    machines: () => ok([]),
    runDigests: () => ok({kind: 'unsupported' as const}),
    onListing: ignore,
    onRecent: ignore,
    onPreferences: ignore,
    onToggleSidebar: ignore,
    onOpenSettings: ignore,
    openSettings: () => ok(null),
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
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {sidebar: {read: () => null, write: ignore}},
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

test('New run opened for a machine starts on it; opened plainly, it keeps its machine', () => {
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {sidebar: {read: () => null, write: ignore}},
  );
  const select = (screen: Screen): void => stores.sidebar.store.dispatch({type: 'select', screen});
  const machine = () => stores.newRun.getState().context.form.machine;
  expect(machine()).toBe('local');
  select({kind: 'machines'});
  select({kind: 'new-run', machine: 'ssh:gpu-box'});
  expect(machine()).toBe('ssh:gpu-box');
  select({kind: 'settings'});
  select({kind: 'new-run'});
  expect(machine()).toBe('ssh:gpu-box');
});

test('regression: Run again opens New run with the options the run was started with', () => {
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {sidebar: {read: () => null, write: ignore}},
  );
  const run = {host: 'ssh:gpu-box', runId: '20261015-163047'} as const;
  stores.sidebar.store.dispatch({
    type: 'recent',
    recent: [
      {
        ...run,
        hostLabel: 'gpu-box',
        project: '/p/queue',
        task: 'mpmc',
        instanceId: null,
        attachedAt: 1,
        start: {
          harness: null,
          options: {rounds: 3, time_limit_s: 600, results: 'publish_github', roles: []},
          args: ['--max-in-flight', '3'],
        },
      },
    ],
  });
  stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'new-run', again: run}});
  const {form} = stores.newRun.getState().context;
  expect(form.machine).toBe('ssh:gpu-box');
  expect(form.task).toBe('mpmc');
  expect(form.budget).toEqual({rounds: 3, timeLimitS: 600, results: 'publish_github'});
  expect(form.advanced).toBe('--max-in-flight 3');
});
