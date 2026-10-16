/**
 * The app shell's composition: the sidebar, Settings, Machines and New run stores over the welcome
 * bridge, the theme following the Settings store, and the shell rendering the selected screen: New
 * run, Settings, Machines, the Run screen for a selected run, or a placeholder for a screen not
 * built yet.
 */
import {
  AppShell,
  createSidebarStore,
  PlaceholderScreen,
  WORDMARK,
} from '@vibesys/feature-app-shell/renderer';
import type {Screen, SidebarState} from '@vibesys/feature-app-shell/state';
import {createMachinesStore, MachinesScreen} from '@vibesys/feature-machines/renderer';
import {createNewRunStore, machineOptions, NewRunScreen} from '@vibesys/feature-new-run/renderer';
import {
  type RunPageNavigation,
  type RunPagePage,
  RunScreen,
  useRunPage,
} from '@vibesys/feature-run-page/renderer';
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
import {applyTheme, Card, EmptyState, Page, type ThemeTarget, Wordmark} from '@vibesys/ui';
import {type JSX, useMemo, useSyncExternalStore} from 'react';

/** The clock the shell's screens time their bounds on: the window's own timers, in `main.tsx`. */
export type Schedule = (callback: () => void, delayMs: number) => () => void;

/** The stores the shell renders, and what its screens reach the app through. */
export interface ShellStores {
  readonly sidebar: ReturnType<typeof createSidebarStore>;
  readonly settings: ReturnType<typeof createSettingsStore>;
  readonly bridge: WelcomeBridge;
  readonly schedule: Schedule;
  readonly machines: ReturnType<typeof createMachinesStore>;
  readonly newRun: ReturnType<typeof createNewRunStore>['store'];
  /** The page a run's live session runs in. */
  readonly page: RunPagePage;
  readonly newRunCompletion: ReturnType<typeof createNewRunStore>['completion'];
}

const DEFAULT_APPEARANCE = {mode: 'system', light: 'paper', dark: 'ink'} as const;

/** What the Run screen cannot do from this window yet, and why; it shows those actions disabled. */
const RUN_UNAVAILABLE = {
  'open-window': 'Opening a run in its own window arrives with multiple windows.',
} as const;

/**
 * Build the shell's stores over `bridge`, and keep the page's theme on what the Settings store
 * shows (the preload already painted the first frame in the stored theme). New run hears the runs
 * this user opened from the sidebar, and a run it starts is shown on its Run screen.
 */
export function createShellStores(
  bridge: WelcomeBridge,
  system: SystemAppearance,
  document: ThemeTarget,
  schedule: Schedule,
  page: RunPagePage,
): ShellStores {
  const settings = createSettingsStore(bridge, system);
  const follow = (): void => {
    const state = settings.getState();
    if (state.kind === 'ready') applyTheme(shownTheme(state, DEFAULT_APPEARANCE), document);
  };
  settings.subscribe(follow);
  const sidebar = createSidebarStore(bridge);
  const newRun = createNewRunStore(bridge, {
    recent: sidebar.store.getState().recent,
    schedule,
    started: run => sidebar.store.dispatch({type: 'select', screen: {kind: 'run', run}}),
  });
  let {recent, selection} = sidebar.store.getState();
  sidebar.store.subscribe(() => {
    const next = sidebar.store.getState();
    if (next.recent !== recent) {
      recent = next.recent;
      newRun.store.dispatch({type: 'recent', recent});
    }
    if (next.selection !== selection) {
      selection = next.selection;
      followMachine(newRun.store, selection);
    }
  });
  return {
    sidebar,
    settings,
    machines: createMachinesStore(bridge),
    newRun: newRun.store,
    newRunCompletion: newRun.completion,
    bridge,
    schedule,
    page,
  };
}

/** New run opened for a machine (Machines' "New run here") starts on it. */
function followMachine(newRun: ShellStores['newRun'], selection: Screen): void {
  if (selection.kind !== 'new-run' || selection.machine === undefined) return;
  if (newRun.getState().context.form.machine === selection.machine) return;
  newRun.dispatch({type: 'machine', machine: selection.machine});
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
  };
  const page = useRunPage({
    seed,
    record,
    bridge: stores.bridge,
    navigation,
    schedule: stores.schedule,
    page: stores.page,
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
  return <RunScreen state={page.state} run={page.run} dispatch={page.dispatch} />;
}

/** The window: the sidebar and the selected screen. */
export function Shell(props: {
  readonly stores: ShellStores;
  readonly version: string;
}): JSX.Element {
  const {sidebar, settings, machines, newRun, newRunCompletion} = props.stores;
  const sidebarState = useStore(sidebar.store);
  const settingsState = useStore(settings);
  const machinesState = useStore(machines);
  const newRunState = useStore(newRun);
  const completionState = useStore(newRunCompletion);
  const select = (screen: Screen): void => sidebar.store.dispatch({type: 'select', screen});
  const screen = (selected: Screen): JSX.Element => {
    switch (selected.kind) {
      case 'new-run':
        return (
          <NewRunScreen
            snapshot={newRunState}
            dispatch={newRun.dispatch}
            machines={machineOptions([
              ...machinesState.machines.map(machine => machine.host),
              ...sidebarState.listings.map(listing => listing.host),
            ])}
            completion={{state: completionState, dispatch: newRunCompletion.dispatch}}
            hero={<Wordmark art={WORDMARK} label="VibeSys" align="center" tone="accent" />}
            chooseCheckout={() => select({kind: 'machines'})}
          />
        );
      case 'settings':
        return <SettingsScreen state={settingsState} dispatch={settings.dispatch} />;
      case 'machines':
        return (
          <MachinesScreen
            state={machinesState}
            dispatch={machines.dispatch}
            newRun={host => select({kind: 'new-run', machine: host})}
          />
        );
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
