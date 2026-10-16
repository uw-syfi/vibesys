/**
 * The app shell's composition: the sidebar store and the Settings store over the welcome bridge,
 * the theme following the Settings store, and the shell rendering the selected screen.
 */
import {AppShell, createSidebarStore, PlaceholderScreen} from '@vibesys/feature-app-shell/renderer';
import type {Screen} from '@vibesys/feature-app-shell/state';
import {
  createSettingsStore,
  SettingsScreen,
  type SystemAppearance,
} from '@vibesys/feature-settings/renderer';
import {shownTheme} from '@vibesys/feature-settings/state';
import type {WelcomeBridge} from '@vibesys/ipc';
import type {Store} from '@vibesys/store';
import {applyTheme, type ThemeTarget} from '@vibesys/ui';
import {type JSX, useSyncExternalStore} from 'react';

/** The stores the shell renders, and what its row menus do. */
export interface ShellStores {
  readonly sidebar: ReturnType<typeof createSidebarStore>;
  readonly settings: ReturnType<typeof createSettingsStore>;
}

const DEFAULT_APPEARANCE = {mode: 'system', light: 'paper', dark: 'ink'} as const;

/**
 * Build the shell's stores over `bridge`, and keep the page's theme on what the Settings store
 * shows (the preload already painted the first frame in the stored theme).
 */
export function createShellStores(
  bridge: WelcomeBridge,
  system: SystemAppearance,
  document: ThemeTarget,
): ShellStores {
  const settings = createSettingsStore(bridge, system);
  const follow = (): void => {
    const state = settings.getState();
    if (state.kind === 'ready') applyTheme(shownTheme(state, DEFAULT_APPEARANCE), document);
  };
  settings.subscribe(follow);
  return {sidebar: createSidebarStore(bridge), settings};
}

function useStore<S, A>(store: Store<S, A>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

/** The window: the sidebar and the selected screen. */
export function Shell(props: {
  readonly stores: ShellStores;
  readonly version: string;
}): JSX.Element {
  const {sidebar, settings} = props.stores;
  const sidebarState = useStore(sidebar.store);
  const settingsState = useStore(settings);
  const screen = (selected: Screen): JSX.Element =>
    selected.kind === 'settings' ? (
      <SettingsScreen state={settingsState} dispatch={settings.dispatch} />
    ) : (
      <PlaceholderScreen screen={selected} />
    );
  return (
    <AppShell
      state={sidebarState}
      dispatch={sidebar.store.dispatch}
      actions={sidebar.actions}
      version={props.version}
      screen={screen}
    />
  );
}
