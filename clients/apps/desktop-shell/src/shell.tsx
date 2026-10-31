/**
 * The app shell's composition: the runs, Settings, Machines, New run and dashboard grouping stores
 * over the welcome bridge, the theme following the Settings store, and the shell rendering the
 * rail and the selected screen: the Runs dashboard (home), New run, Settings, Machines, Usage, or
 * the Run screen for a selected run.
 */
import {
  AppShell,
  createSidebarStore,
  type WindowAddress,
} from '@vibesys/feature-app-shell/renderer';
import {
  type Clock,
  type DashboardRow,
  dashboardRows,
  digestKey,
  machineLoads,
  projectRunGaps,
  recentNotice,
  runLabels,
  runProjectRoot,
  runsKnown,
  type Screen,
  type SidebarState,
} from '@vibesys/feature-app-shell/state';
import {
  createMachinesStore,
  MachinesPage,
  MachinesScreen,
} from '@vibesys/feature-machines/renderer';
import {
  type MachinesState,
  machineCounts,
  machinesAttention,
  machinesProblems,
} from '@vibesys/feature-machines/state';
import {
  createNewRunStore,
  type DraftMemory,
  draftWords,
  type MoreOptionsMemory,
  machineOptions,
  NewRunScreen,
  type NewRunSnapshot,
} from '@vibesys/feature-new-run/renderer';
import type {
  RunPageAddress,
  RunPageNavigation,
  RunPagePage,
} from '@vibesys/feature-run-page/renderer';
import {RUN_ROUTE, routeQuery} from '@vibesys/feature-run-page/state';
import {
  createGroupingStore,
  type GroupingStore,
  type MachineSteps,
  type RunsDraft,
  RunsScreen,
} from '@vibesys/feature-runs-dashboard/renderer';
import {needsYou, type Tile, tilesOf} from '@vibesys/feature-runs-dashboard/state';
import {
  createMachineSettingsStore,
  createSettingsStore,
  SettingsScreen,
  type SystemAppearance,
} from '@vibesys/feature-settings/renderer';
import {
  machineSettingsProblems,
  settingsProblems,
  shownTheme,
} from '@vibesys/feature-settings/state';
import {createUsageStore, type SpendRunRef, UsageScreen} from '@vibesys/feature-usage/renderer';
import {type HostKey, hostFromKey, hostLabel, hostsComeUp, problemWord} from '@vibesys/hosts';
import {gatewayBridge, settled, type WelcomeBridge} from '@vibesys/ipc';
import {createProblemBoard, type ProblemBoard, reportProblems} from '@vibesys/problems';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {sameRun} from '@vibesys/runs';
import type {Store} from '@vibesys/store';
import {applyTheme, Card, EmptyState, Page, type ThemeTarget, WindowFrame} from '@vibesys/ui';
import {type JSX, useMemo, useSyncExternalStore} from 'react';
import {GatewayRun} from './gateway-run.js';
import {loadingMachines, runCounts, runInput} from './shell-runs.js';

/** The clock the shell's screens time their bounds on: the window's own timers, in `main.tsx`. */
export type Schedule = (callback: () => void, delayMs: number) => () => void;

/** The page the shell runs in; its address holds the window's location. */
export interface ShellPage extends Omit<RunPagePage, 'address'> {
  readonly address: WindowAddress & Pick<RunPageAddress, 'onEscape'>;
}

/** The stores the shell renders, and what its screens reach the app through. */
export interface ShellStores {
  readonly sidebar: ReturnType<typeof createSidebarStore>;
  /** What the Runs dashboard groups its tiles by. */
  readonly grouping: GroupingStore;
  readonly settings: ReturnType<typeof createSettingsStore>;
  /** What each machine has set up (clusters, clouds, agent logins), and the Settings section shown. */
  readonly machineSettings: ReturnType<typeof createMachineSettingsStore>;
  readonly bridge: WelcomeBridge;
  readonly schedule: Schedule;
  readonly machines: ReturnType<typeof createMachinesStore>;
  /** What the Usage page shows; it asks only while the page is on screen. */
  readonly usage: ReturnType<typeof createUsageStore>;
  readonly newRun: ReturnType<typeof createNewRunStore>['store'];
  /** The page a run's live session runs in. */
  readonly page: ShellPage;
  readonly newRunCompletion: ReturnType<typeof createNewRunStore>['completion'];
  /** The problems the window's screens report, shown as toasts by `ProblemHost`. */
  readonly problems: ProblemBoard;
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
      runs: () => stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'runs'}}),
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
  page: ShellPage,
  preferences: ShellPreferences,
): ShellStores {
  const settings = createSettingsStore(bridge, system);
  const follow = (): void => {
    const state = settings.getState();
    if (state.kind === 'ready') applyTheme(shownTheme(state, DEFAULT_APPEARANCE), document);
  };
  settings.subscribe(follow);
  const sidebar = createSidebarStore(bridge, schedule, page.address);
  const newRun = createNewRunStore(bridge, {
    recent: sidebar.store.getState().recent,
    schedule,
    ...(preferences.moreOptions === undefined ? {} : {moreOptions: preferences.moreOptions}),
    ...(preferences.draft === undefined ? {} : {draft: preferences.draft}),
    started: run => sidebar.store.dispatch({type: 'select', screen: {kind: 'run', run}}),
  });
  const machineSettings = createMachineSettingsStore(bridge);
  const machines = createMachinesStore(bridge);
  const problems = createProblemBoard();
  reportProblems(problems, 'settings', settings, settingsProblems);
  reportProblems(problems, 'machines', machines, machinesProblems);
  reportProblems(problems, 'machine-settings', machineSettings, state =>
    machineSettingsProblems(state, host => hostLabel(hostFromKey(host))),
  );
  let listed = '';
  machines.subscribe(() => {
    const hosts = machines.getState().machines.map(machine => machine.host);
    if (hosts.join('\n') === listed) return;
    listed = hosts.join('\n');
    machineSettings.dispatch({type: 'hosts', hosts});
  });
  followCompute(newRun.store, machineSettings);
  rereadOnReconnect(sidebar.store, machineSettings);
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
    grouping: createGroupingStore(),
    settings,
    machineSettings,
    machines,
    usage: createUsageStore(bridge, {
      schedule,
      clock: () => {
        const now = page.now();
        return {now, midnight: new Date(now).setHours(0, 0, 0, 0)};
      },
    }),
    newRun: newRun.store,
    newRunCompletion: newRun.completion,
    bridge,
    schedule,
    page,
    problems,
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
 * A machine's Settings reading that failed because the machine was unreachable is read again when
 * its connection comes up, so the user need not press Retry.
 */
function rereadOnReconnect(
  sidebar: ShellStores['sidebar']['store'],
  machineSettings: ShellStores['machineSettings'],
): void {
  let listings = sidebar.getState().listings;
  sidebar.subscribe(() => {
    const next = sidebar.getState().listings;
    const up = hostsComeUp(listings, next);
    listings = next;
    for (const host of up) {
      const view = machineSettings.getState().views[host];
      for (const what of ['compute', 'logins'] as const) {
        if (view?.[what].kind === 'failed') machineSettings.dispatch({type: 'refresh', host, what});
      }
    }
  });
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

/** The window's location as `run`'s page sees it: the zoom it holds while it shows that run. */
function runAddress(stores: ShellStores, run: RunRef): RunPageAddress {
  const {store} = stores.sidebar;
  return {
    zoom: () => {
      const shown = store.getState().selection;
      return shown.kind === 'run' && sameRun(shown.run, run) ? (shown.zoom ?? '') : '';
    },
    move: zoom => store.dispatch({type: 'select', screen: {kind: 'run', run, zoom}}),
    subscribe: store.subscribe,
    onEscape: stores.page.address.onEscape,
  };
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
  const projectRoot = runProjectRoot(sidebar, run);
  const project = projectRoot ?? '';
  const unavailableWindow = place.unavailable['open-window'];
  const seed = useMemo(
    () => ({
      identity: {host: run.host, runId: run.runId},
      title: {project, task: recent?.task ?? null},
      hostLabel: hostLabel(hostFromKey(run.host)),
      unavailable: unavailableWindow === undefined ? {} : {'open-window': unavailableWindow},
    }),
    [run.host, run.runId, project, recent?.task, unavailableWindow],
  );
  const ports = typeof window === 'undefined' ? undefined : window.vibesysGateway;
  const gateway = useMemo(() => (ports === undefined ? undefined : gatewayBridge(ports)), [ports]);
  const address = useMemo(() => runAddress(stores, run), [stores, run]);
  return gateway === undefined ? (
    <Page>
      <Card>
        <EmptyState title="Run service unavailable" />
      </Card>
    </Page>
  ) : (
    <GatewayRun
      key={`${run.host}/${run.runId}`}
      seed={seed}
      projectRoot={projectRoot}
      bridge={gateway}
      schedule={stores.schedule}
      address={address}
      navigation={place.navigation(run)}
      problems={stores.problems}
    />
  );
}

/** The unfinished New run the dashboard offers, or null when there is not one. */
function runsDraft(
  state: NewRunSnapshot,
  select: (screen: Screen) => void,
  dispatch: ShellStores['newRun']['dispatch'],
): RunsDraft | null {
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

/** New run over its store, offered the machines saved and the ones listing runs. */
function NewRunRoute(props: {
  readonly stores: ShellStores;
  readonly sidebar: SidebarState;
  readonly machines: MachinesState;
  readonly select: (screen: Screen) => void;
}): JSX.Element {
  const {newRun, newRunCompletion} = props.stores;
  const snapshot = useStore(newRun);
  const completion = useStore(newRunCompletion);
  return (
    <NewRunScreen
      snapshot={snapshot}
      dispatch={newRun.dispatch}
      machines={machineOptions(hostsOf(props.machines, props.sidebar))}
      completion={{state: completion, dispatch: newRunCompletion.dispatch}}
      chooseCheckout={() => {
        props.stores.machineSettings.dispatch({type: 'section', section: 'machines'});
        props.select({kind: 'settings'});
      }}
      showRuns={() => props.select({kind: 'runs'})}
      setUp={machine => props.select({kind: 'settings', machine, section: 'clusters'})}
    />
  );
}

/** Settings over its stores, with the Machines screen embedded in it. */
function SettingsRoute(props: {
  readonly stores: ShellStores;
  readonly machines: MachinesState;
  readonly select: (screen: Screen) => void;
}): JSX.Element {
  const {settings, machineSettings, machines} = props.stores;
  const settingsState = useStore(settings);
  const machineSettingsState = useStore(machineSettings);
  const machinesState = props.machines;
  return (
    <SettingsScreen
      state={settingsState}
      dispatch={settings.dispatch}
      back={props.stores.page.address.back}
      machinesContent={
        <MachinesScreen
          embedded
          state={machinesState}
          dispatch={machines.dispatch}
          newRun={host => props.select({kind: 'new-run', machine: host})}
          back={() => props.select({kind: 'settings'})}
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
}

/** The Runs dashboard over the tiles of `rows`: a tile opens its run, and its menu archives it or opens it in a window. */
function RunsRoute(props: {
  readonly rows: readonly DashboardRow[];
  readonly tiles: readonly Tile[];
  readonly stores: ShellStores;
  readonly draft: RunsDraft | null;
  readonly loads: readonly MachineSteps[];
  readonly select: (screen: Screen) => void;
}): JSX.Element {
  const {rows, stores, select} = props;
  const {by, machine} = useStore(stores.grouping);
  const gaps = projectRunGaps(useStore(stores.sidebar.store).allRuns);
  const runOf = (id: string): RunRef | null =>
    rows.find(each => digestKey(each.row.run) === id)?.row.run ?? null;
  return (
    <RunsScreen
      tiles={props.tiles}
      by={by}
      machine={machine}
      clearMachine={() => stores.grouping.dispatch({type: 'clear-machine'})}
      group={next => stores.grouping.dispatch({type: 'group', by: next})}
      open={id => {
        const run = runOf(id);
        if (run !== null) select({kind: 'run', run});
      }}
      menu={id => {
        const run = runOf(id);
        if (run === null) return [];
        const {actions} = stores.sidebar;
        return [
          {id: 'window', label: 'Open in new window', press: () => actions.openWindow(run)},
          {id: 'archive', label: 'Archive', press: () => actions.archive(run)},
        ];
      }}
      draft={props.draft}
      loads={props.loads}
      newRun={() => select({kind: 'new-run'})}
      notice={
        gaps.length === 0
          ? recentNotice(stores.sidebar.store.getState().skippedRecent)
          : {
              title: 'Run list incomplete',
              body: gaps
                .map(
                  gap =>
                    `${hostLabel(hostFromKey(gap.host))}: ${gap.project}: ${gap.refusal?.detail ?? 'Reading runs'}`,
                )
                .join('; '),
            }
      }
    />
  );
}

/** The Machines page over its store: a machine's runs are the dashboard narrowed to it, its Settings open on its clusters. */
function MachinesRoute(props: {
  readonly stores: ShellStores;
  readonly machines: MachinesState;
  readonly counts: ReadonlyMap<HostKey, number>;
  readonly select: (screen: Screen) => void;
}): JSX.Element {
  const {stores, select} = props;
  return (
    <MachinesPage
      state={props.machines}
      dispatch={stores.machines.dispatch}
      runCounts={props.counts}
      incompleteHosts={
        new Set(projectRunGaps(useStore(stores.sidebar.store).allRuns).map(gap => gap.host))
      }
      showRuns={host => {
        stores.grouping.dispatch({
          type: 'filter-machine',
          machine: hostLabel(hostFromKey(host)),
        });
        select({kind: 'runs'});
      }}
      openSettings={host => select({kind: 'settings', machine: host, section: 'clusters'})}
    />
  );
}

/** The Usage page over its store; a run's row opens that run on its Usage face. */
function UsageRoute(props: {
  readonly stores: ShellStores;
  readonly sidebar: SidebarState;
  readonly select: (screen: Screen) => void;
}): JSX.Element {
  const {stores, select} = props;
  const {store, actions} = stores.usage;
  const state = useStore(store);
  const label = useMemo(() => runLabels(props.sidebar, viewerClock()), [props.sidebar]);
  return (
    <UsageScreen
      state={state}
      actions={actions}
      label={label}
      openRun={(run: SpendRunRef) => {
        select({
          kind: 'run',
          run: {host: run.host, runId: run.runId},
          zoom: routeQuery({...RUN_ROUTE, mode: 'usage'}),
        });
      }}
    />
  );
}

/** The window: the rail and the selected screen. */
export function Shell(props: {
  readonly stores: ShellStores;
  readonly version: string;
}): JSX.Element {
  const {sidebar, machines, newRun} = props.stores;
  const sidebarState = useStore(sidebar.store);
  const machinesState = useStore(machines);
  const newRunState = useStore(newRun);
  const rows = useMemo(() => dashboardRows(sidebarState, viewerClock()), [sidebarState]);
  const tiles = useMemo(() => tilesOf(rows.map(row => runInput(row, Date.now()))), [rows]);
  const counts = useMemo(() => runCounts(rows), [rows]);
  const select = (screen: Screen): void => sidebar.store.dispatch({type: 'select', screen});
  const machinesListed = machineCounts(machinesState).listed;
  const saved = machinesListed ? machinesState.machines.map(each => each.host) : null;
  const known = runsKnown(sidebarState, saved);
  // The window never changes screen by itself, on a clock or on what a machine says: Runs reads
  // while the machines answer and says so when they hold no run. Only the mark, pressed, goes to
  // New run when none is known.
  const home: Screen = known === 'none' ? {kind: 'new-run'} : {kind: 'runs'};
  const draft = runsDraft(newRunState, select, newRun.dispatch);
  const screen = (selected: Screen): JSX.Element => {
    switch (selected.kind) {
      case 'new-run':
        return (
          <NewRunRoute
            stores={props.stores}
            sidebar={sidebarState}
            machines={machinesState}
            select={select}
          />
        );
      case 'settings':
        return <SettingsRoute stores={props.stores} machines={machinesState} select={select} />;
      case 'machines':
        return (
          <MachinesRoute
            stores={props.stores}
            machines={machinesState}
            counts={counts}
            select={select}
          />
        );
      case 'runs':
        return (
          <RunsRoute
            rows={rows}
            tiles={tiles}
            stores={props.stores}
            draft={draft}
            loads={loadingMachines(machineLoads(sidebarState, saved), sidebar.actions.retryLoad)}
            select={select}
          />
        );
      case 'usage':
        return <UsageRoute stores={props.stores} sidebar={sidebarState} select={select} />;
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
    }
  };
  const attention = machinesAttention(machinesState);
  return (
    <AppShell
      state={sidebarState}
      dispatch={sidebar.store.dispatch}
      version={props.version}
      badges={{
        needsYou: needsYou(tiles).length,
        machineWarning: attention === null ? null : problemWord(attention),
      }}
      home={home}
      screen={screen}
    />
  );
}

/** The moment Recent's times are worded against: now, in the viewer's own zone. */
const viewerClock = (): Clock => ({now: Date.now()});

/**
 * A run window: the Run screen for `run` alone, with no rail, in the same frame as the main window's
 * so the window's own controls keep the same room. The sidebar's store still runs, for the hosts'
 * listings the run's record is read from.
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
