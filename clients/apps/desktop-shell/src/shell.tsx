/**
 * The app shell's composition: the sidebar store and the Settings store over the welcome bridge,
 * the theme following the Settings store, and the shell rendering the selected screen: Settings,
 * the Run screen for a selected run, or a placeholder for a screen not built yet.
 */
import {AppShell, createSidebarStore, PlaceholderScreen} from '@vibesys/feature-app-shell/renderer';
import type {Screen, SidebarState} from '@vibesys/feature-app-shell/state';
import {type RunPageNavigation, RunScreen, useRunPage} from '@vibesys/feature-run-page/renderer';
import {runRecord} from '@vibesys/feature-run-page/state';
import {
  createSettingsStore,
  SettingsScreen,
  type SystemAppearance,
} from '@vibesys/feature-settings/renderer';
import {shownTheme} from '@vibesys/feature-settings/state';
import {hostFromKey, hostLabel} from '@vibesys/hosts';
import type {WelcomeBridge} from '@vibesys/ipc';
import type {RunRef} from '@vibesys/runs';
import {sameRun} from '@vibesys/runs';
import type {Store} from '@vibesys/store';
import {applyTheme, Card, EmptyState, Page, type ThemeTarget} from '@vibesys/ui';
import {type JSX, useMemo, useSyncExternalStore} from 'react';

/** The clock the shell's screens time their bounds on: the window's own timers, in `main.tsx`. */
export type Schedule = (callback: () => void, delayMs: number) => () => void;

/** The stores the shell renders, and what its screens reach the app through. */
export interface ShellStores {
  readonly sidebar: ReturnType<typeof createSidebarStore>;
  readonly settings: ReturnType<typeof createSettingsStore>;
  readonly bridge: WelcomeBridge;
  readonly schedule: Schedule;
}

const DEFAULT_APPEARANCE = {mode: 'system', light: 'paper', dark: 'ink'} as const;

/** What the Run screen cannot do from this window yet, and why; it shows those actions disabled. */
const RUN_UNAVAILABLE = {
  pause: 'Pausing from this window arrives with the live run connection.',
  'open-window': 'Opening a run in its own window arrives with multiple windows.',
} as const;

/**
 * Build the shell's stores over `bridge`, and keep the page's theme on what the Settings store
 * shows (the preload already painted the first frame in the stored theme).
 */
export function createShellStores(
  bridge: WelcomeBridge,
  system: SystemAppearance,
  document: ThemeTarget,
  schedule: Schedule,
): ShellStores {
  const settings = createSettingsStore(bridge, system);
  const follow = (): void => {
    const state = settings.getState();
    if (state.kind === 'ready') applyTheme(shownTheme(state, DEFAULT_APPEARANCE), document);
  };
  settings.subscribe(follow);
  return {sidebar: createSidebarStore(bridge), settings, bridge, schedule};
}

function useStore<S, A>(store: Store<S, A>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

/** The Run screen for `run`, fed its record from the sidebar's listings. */
function RunRoute(props: {
  readonly run: RunRef;
  readonly sidebar: SidebarState;
  readonly stores: ShellStores;
}): JSX.Element {
  const {run, sidebar, stores} = props;
  const record = useMemo(() => runRecord(sidebar.listings, run), [sidebar.listings, run]);
  const recent = sidebar.recent.find(each => sameRun(each, run));
  const project = recent?.project ?? (record.kind === 'unlisted' ? '' : record.run.projectRoot);
  const seed = {
    identity: run,
    title: {project, task: recent?.task ?? null},
    hostLabel: hostLabel(hostFromKey(run.host)),
    unavailable: RUN_UNAVAILABLE,
  };
  const ignore = (): void => {};
  const navigation: RunPageNavigation = {
    runAgain: () => stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'new-run'}}),
    openWindow: ignore,
    pause: ignore,
    resume: ignore,
  };
  const page = useRunPage({
    seed,
    record,
    bridge: stores.bridge,
    navigation,
    schedule: stores.schedule,
  });
  if (page === null) {
    return (
      <Page>
        <Card>
          <EmptyState title="Opening the run" />
        </Card>
      </Page>
    );
  }
  return <RunScreen state={page.state} dispatch={page.dispatch} />;
}

/** The window: the sidebar and the selected screen. */
export function Shell(props: {
  readonly stores: ShellStores;
  readonly version: string;
}): JSX.Element {
  const {sidebar, settings} = props.stores;
  const sidebarState = useStore(sidebar.store);
  const settingsState = useStore(settings);
  const screen = (selected: Screen): JSX.Element => {
    switch (selected.kind) {
      case 'settings':
        return <SettingsScreen state={settingsState} dispatch={settings.dispatch} />;
      case 'run':
        return (
          <RunRoute
            key={`${selected.run.host} ${selected.run.runId}`}
            run={selected.run}
            sidebar={sidebarState}
            stores={props.stores}
          />
        );
      default:
        return <PlaceholderScreen screen={selected} />;
    }
  };
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
