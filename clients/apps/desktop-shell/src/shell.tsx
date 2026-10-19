/**
 * The app shell's composition: the sidebar, Settings, Machines and New run stores over the welcome
 * bridge, the theme following the Settings store, and the shell rendering the selected screen: New
 * run, Settings, Machines, All runs, the Run screen for a selected run, or a placeholder for a screen not
 * built yet.
 */
import {
  AllRunsScreen,
  AppShell,
  createSidebarStore,
  PlaceholderScreen,
  type SidebarDraft,
  type SidebarPreference,
} from '@vibesys/feature-app-shell/renderer';
import {
  allRunsGroups,
  type Clock,
  type Screen,
  type SidebarState,
  sidebarShortcutLabel,
} from '@vibesys/feature-app-shell/state';
import {createMachinesStore, MachinesScreen} from '@vibesys/feature-machines/renderer';
import {machineCounts} from '@vibesys/feature-machines/state';
import {
  createNewRunStore,
  type DraftMemory,
  draftWords,
  type MoreOptionsMemory,
  machineOptions,
  NewRunScreen,
  type NewRunSnapshot,
} from '@vibesys/feature-new-run/renderer';
import {
  type RunPageNavigation,
  type RunPagePage,
  RunScreen,
  useRunPage,
} from '@vibesys/feature-run-page/renderer';
import {runRecord} from '@vibesys/feature-run-page/state';
import {
  createMachineSettingsStore,
  createSettingsStore,
  SettingsScreen,
  type SystemAppearance,
} from '@vibesys/feature-settings/renderer';
import {shownTheme} from '@vibesys/feature-settings/state';
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {sameRun} from '@vibesys/runs';
import type {Store} from '@vibesys/store';
import {applyTheme, Card, EmptyState, Page, type ThemeTarget, WindowFrame} from '@vibesys/ui';
import {type JSX, useMemo, useSyncExternalStore} from 'react';

/** The clock the shell's screens time their bounds on: the window's own timers, in `main.tsx`. */
export type Schedule = (callback: () => void, delayMs: number) => () => void;

/** The stores the shell renders, and what its screens reach the app through. */
export interface ShellStores {
  readonly sidebar: ReturnType<typeof createSidebarStore>;
  readonly settings: ReturnType<typeof createSettingsStore>;
  /** What each machine has set up (clusters, clouds, agent logins), and the Settings section shown. */
  readonly machineSettings: ReturnType<typeof createMachineSettingsStore>;
  readonly bridge: WelcomeBridge;
  readonly schedule: Schedule;
  readonly machines: ReturnType<typeof createMachinesStore>;
  readonly newRun: ReturnType<typeof createNewRunStore>['store'];
  /** The page a run's live session runs in. */
  readonly page: RunPagePage;
  readonly newRunCompletion: ReturnType<typeof createNewRunStore>['completion'];
}

const DEFAULT_APPEARANCE = {mode: 'system', light: 'paper', dark: 'ink'} as const;

/**
 * How the Run screen fits its window: in the main one it may open its run in a window of its own,
 * and Run again goes to New run; in a run window (already its own) it may not, and Run again
 * brings the main window, where New run is, to the front.
 */
interface RunPlace {
  readonly unavailable: Partial<Record<'open-window', string>>;
  navigation(run: RunRef): RunPageNavigation;
}

function mainWindowPlace(stores: ShellStores): RunPlace {
  return {
    unavailable: {},
    navigation: run => ({
      runAgain: () =>
        stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'new-run', again: run}}),
      openWindow: () => stores.sidebar.actions.openWindow(run),
    }),
  };
}

function runWindowPlace(stores: ShellStores): RunPlace {
  return {
    unavailable: {'open-window': 'This run is already in a window of its own.'},
    navigation: () => ({
      runAgain: () => void settled(stores.bridge.showMainWindow()),
      openWindow: () => {},
    }),
  };
}

/** What the shell keeps between runs of the app, each in the viewer's own storage. */
export interface ShellPreferences {
  readonly sidebar: SidebarPreference;
  /** What the user chose for New run's More options on each machine; none keeps it this run only. */
  readonly moreOptions?: MoreOptionsMemory;
  /** This window's unfinished New run, so leaving and coming back finds it; none keeps it this run only. */
  readonly draft?: DraftMemory;
}

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
  preferences: ShellPreferences,
): ShellStores {
  const settings = createSettingsStore(bridge, system);
  const follow = (): void => {
    const state = settings.getState();
    if (state.kind === 'ready') applyTheme(shownTheme(state, DEFAULT_APPEARANCE), document);
  };
  settings.subscribe(follow);
  const sidebar = createSidebarStore(bridge, preferences.sidebar, schedule);
  const newRun = createNewRunStore(bridge, {
    recent: sidebar.store.getState().recent,
    schedule,
    ...(preferences.moreOptions === undefined ? {} : {moreOptions: preferences.moreOptions}),
    ...(preferences.draft === undefined ? {} : {draft: preferences.draft}),
    started: run => sidebar.store.dispatch({type: 'select', screen: {kind: 'run', run}}),
  });
  const machineSettings = createMachineSettingsStore(bridge);
  const machines = createMachinesStore(bridge);
  let listed = '';
  machines.subscribe(() => {
    const hosts = machines.getState().machines.map(machine => machine.host);
    if (hosts.join('\n') === listed) return;
    listed = hosts.join('\n');
    machineSettings.dispatch({type: 'hosts', hosts});
  });
  followCompute(newRun.store, machineSettings);
  let {recent, selection} = sidebar.store.getState();
  sidebar.store.subscribe(() => {
    const next = sidebar.store.getState();
    if (next.recent !== recent) {
      recent = next.recent;
      newRun.store.dispatch({type: 'recent', recent});
    }
    if (next.selection !== selection) {
      selection = next.selection;
      followSelection(newRun.store, selection, next.recent);
      if (selection.kind === 'settings' && selection.machine !== undefined) {
        machineSettings.dispatch({
          type: 'focus',
          host: selection.machine,
          section: selection.section ?? 'clusters',
        });
      }
    }
  });
  return {
    sidebar,
    settings,
    machineSettings,
    machines,
    newRun: newRun.store,
    newRunCompletion: newRun.completion,
    bridge,
    schedule,
    page,
  };
}

/**
 * What the machine New run is for has set up to measure on is what its Settings data says: that
 * data is read once for the machine, and each change of it (a cluster added in Settings) reaches
 * New run as an event, so the two never disagree.
 */
function followCompute(
  newRun: ShellStores['newRun'],
  machineSettings: ShellStores['machineSettings'],
): void {
  const follow = (): void => {
    const machine = newRun.getState().context.form.machine;
    const view = machineSettings.getState().views[machine]?.compute;
    if (view === undefined) {
      machineSettings.dispatch({type: 'refresh', host: machine, what: 'compute'});
      return;
    }
    const compute = view.kind === 'ready' ? view.offered : null;
    if (newRun.getState().context.compute !== compute) {
      newRun.dispatch({type: 'compute', machine, compute});
    }
  };
  newRun.subscribe(follow);
  machineSettings.subscribe(follow);
  follow();
}

/**
 * New run opened for a machine (Machines' "New run here") starts on it; opened to run a run again,
 * it starts from that run as the recent list records it (its place and options).
 */
function followSelection(
  newRun: ShellStores['newRun'],
  selection: Screen,
  recent: readonly RecentRun[],
): void {
  if (selection.kind !== 'new-run') return;
  const again = selection.again;
  if (again !== undefined) {
    const run = recent.find(each => sameRun(each, again));
    if (run !== undefined) {
      newRun.dispatch({type: 'again', run});
      return;
    }
    if (newRun.getState().context.form.machine !== again.host) {
      newRun.dispatch({type: 'machine', machine: again.host});
    }
    return;
  }
  if (selection.machine === undefined) return;
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
  readonly place: RunPlace;
}): JSX.Element {
  const {run, sidebar, stores, place} = props;
  const recent = sidebar.recent.find(each => sameRun(each, run));
  const record = useMemo(
    () => runRecord(sidebar.listings, run, recent),
    [sidebar.listings, run, recent],
  );
  const project =
    recent?.project ??
    (record.kind === 'live' || record.kind === 'ended' ? record.run.projectRoot : '');
  const seed = {
    identity: run,
    title: {project, task: recent?.task ?? null},
    hostLabel: hostLabel(hostFromKey(run.host)),
    unavailable: place.unavailable,
  };
  const navigation = place.navigation(run);
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
  return (
    <RunScreen
      state={page.state}
      model={page.model}
      whole={page.whole}
      core={page.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={page.dispatch}
    />
  );
}

/** The All runs screen over the sidebar's store and its state. */
function AllRuns(props: {
  readonly sidebar: ShellStores['sidebar'];
  readonly state: SidebarState;
}): JSX.Element {
  return (
    <AllRunsScreen
      groups={allRunsGroups(props.state, viewerClock())}
      state={props.state}
      dispatch={props.sidebar.store.dispatch}
      actions={props.sidebar.actions}
    />
  );
}

/** The unfinished New run the sidebar lists, or null when there is not one. */
function sidebarDraft(
  state: NewRunSnapshot,
  select: (screen: Screen) => void,
  dispatch: ShellStores['newRun']['dispatch'],
): SidebarDraft | null {
  const words = draftWords(state.context);
  if (words === null) return null;
  return {
    ...words,
    open: () => select({kind: 'new-run'}),
    discard: () => dispatch({type: 'reset'}),
  };
}

/** The machines the app knows: the saved ones, then those with runs listed. */
function hostsOf(
  machines: {readonly machines: readonly {readonly host: HostKey}[]},
  sidebar: {readonly listings: readonly {readonly host: HostKey}[]},
): HostKey[] {
  return [...machines.machines.map(each => each.host), ...sidebar.listings.map(each => each.host)];
}

/** The window: the sidebar and the selected screen. */
export function Shell(props: {
  readonly stores: ShellStores;
  readonly version: string;
}): JSX.Element {
  const {sidebar, settings, machineSettings, machines, newRun, newRunCompletion} = props.stores;
  const sidebarState = useStore(sidebar.store);
  const settingsState = useStore(settings);
  const machinesState = useStore(machines);
  const machineSettingsState = useStore(machineSettings);
  const newRunState = useStore(newRun);
  const completionState = useStore(newRunCompletion);
  const select = (screen: Screen): void => sidebar.store.dispatch({type: 'select', screen});
  const draft = sidebarDraft(newRunState, select, newRun.dispatch);
  const screen = (selected: Screen): JSX.Element => {
    switch (selected.kind) {
      case 'new-run':
        return (
          <NewRunScreen
            snapshot={newRunState}
            dispatch={newRun.dispatch}
            machines={machineOptions(hostsOf(machinesState, sidebarState))}
            completion={{state: completionState, dispatch: newRunCompletion.dispatch}}
            chooseCheckout={() => select({kind: 'machines'})}
            setUp={machine => select({kind: 'settings', machine, section: 'clusters'})}
          />
        );
      case 'settings':
        return (
          <SettingsScreen
            state={settingsState}
            dispatch={settings.dispatch}
            back={() => sidebar.store.dispatch({type: 'back-from-settings'})}
            machinesContent={
              <MachinesScreen
                embedded
                state={machinesState}
                dispatch={machines.dispatch}
                newRun={host => select({kind: 'new-run', machine: host})}
                back={() => select({kind: 'settings'})}
              />
            }
            machines={machineCounts(machinesState)}
            machineSettings={{
              state: machineSettingsState,
              dispatch: machineSettings.dispatch,
              machines: machinesState.machines.map(({host, label}) => ({host, label})),
            }}
          />
        );
      case 'machines':
        return (
          <MachinesScreen
            state={machinesState}
            dispatch={machines.dispatch}
            newRun={host => select({kind: 'new-run', machine: host})}
            back={() => select({kind: 'settings'})}
          />
        );
      case 'all-runs':
        return <AllRuns sidebar={sidebar} state={sidebarState} />;
      case 'run':
        return (
          <RunRoute
            key={`${selected.run.host} ${selected.run.runId}`}
            run={selected.run}
            sidebar={sidebarState}
            stores={props.stores}
            place={mainWindowPlace(props.stores)}
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
      shortcut={sidebarShortcutLabel(props.stores.bridge.platform === 'darwin')}
      clock={viewerClock}
      draft={draft}
      screen={screen}
    />
  );
}

/** The moment Recent's times are worded against: now, in the viewer's own zone. */
const viewerClock = (): Clock => ({now: Date.now()});

/**
 * A run window: the Run screen for `run` alone, with no sidebar, below the same title-bar row as
 * the main window's. The sidebar's store still runs,
 * for the hosts' listings the run's record is read from.
 */
export function RunWindow(props: {
  readonly stores: ShellStores;
  readonly run: RunRef;
}): JSX.Element {
  const sidebarState = useStore(props.stores.sidebar.store);
  return (
    <WindowFrame>
      <RunRoute
        run={props.run}
        sidebar={sidebarState}
        stores={props.stores}
        place={runWindowPlace(props.stores)}
      />
    </WindowFrame>
  );
}
