/**
 * The window bound to the app's services: the rail, and the screen of the destination it shows.
 * The run streams of every host are followed for as long as the window is open, whichever screen
 * shows, so the Machines page reads live connections; the navigation and the appearance are the
 * stores the entry built, and a screen's callbacks become the intents those stores take.
 */
import {useAtomMount, useAtomValue} from '@effect/atom-react';
import type {Appearance} from '@vibesys/ui/tokens';
import * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo, useState} from 'react';
import type {
  AppearanceStore,
  MachineActions,
  MachineFolders,
  NavigationStore,
} from '../../core/index.js';
import {CLIENT_BUILD as DESKTOP_BUILD} from '../../core/index.js';
import {
  type AppearanceState,
  type MachineRow,
  machineRow,
  type Navigation,
  type NavigationAction,
  staleForMs,
  type Viewer,
} from '../../state/index.js';
import {
  AddMachinePage,
  type MachineControls,
  type PlacesHost,
  useMachineControls,
} from '../machines/index.js';
import {nowMs} from '../orchestrator/clock.js';
import {
  ConnectedApp,
  type ConnectedHost,
  type Dispatch,
  type Issues,
  NO_ISSUES,
} from '../orchestrator/connected.js';
import {faultWord, roleWord} from '../orchestrator/words.js';
import type {RepositoriesControls} from '../settings/repositories-card.js';
import {SettingsScreen} from '../settings/settings-screen.js';
import {ConnectedCatchUp} from './connected-catch-up.js';
import {ConnectedCost} from './connected-cost.js';
import {ConnectedMachines} from './connected-machines.js';
import {FocusMemory, placeOf} from './focus-memory.js';
import {AppShell} from './shell.js';

/** The disjoint run-discovery streams of every host, followed while the run navigator exists. */
const watchRuns = (hosts: readonly ConnectedHost[]): Atom.Atom<void> =>
  Atom.make(get => {
    for (const host of hosts) get(host.session.runNavigation);
  });

/** Each host as the Machines page shows it. */
const machinesOf = (hosts: readonly ConnectedHost[]): Atom.Atom<readonly MachineRow[]> =>
  Atom.make(get => {
    const clock = get(nowMs);
    return hosts.map(({key, label, session}) =>
      machineRow({
        key,
        label,
        connection: get(session.connection.state),
        runsFeed: get(session.runs.status),
        hasData: get(session.model).data.sequence > 0n,
        staleForMs: AsyncResult.isSuccess(clock)
          ? staleForMs(get(session.connection.freshness), clock.value)
          : null,
      }),
    );
  });

/** Each host's eval places as the Machines page shows them. */
const placesOf = (hosts: readonly ConnectedHost[]): Atom.Atom<readonly PlacesHost[]> =>
  Atom.make(get =>
    hosts.map(({key, label, session}) => ({key, label, places: get(session.newRun.places)})),
  );

/** The Repositories rows of Settings: each machine with its kept folder, and the save of one. */
function repositoryControls(
  folders: MachineFolders | undefined,
  rows: readonly MachineRow[],
  kept: Readonly<Record<string, string>>,
  dispatch: Dispatch,
): RepositoriesControls | undefined {
  if (folders === undefined) return undefined;
  return {
    rows: rows.map(({key, label}) => ({key, label, folder: kept[key] ?? ''})),
    save: (key, folder, done) =>
      dispatch(folders.save(key, folder), outcome =>
        done(outcome.kind === 'saved' ? null : outcome.reason),
      ),
  };
}

/** The Refresh of one machine's places: the host probes them again. */
const refreshPlaces =
  (hosts: readonly ConnectedHost[], dispatch: Dispatch) =>
  (key: string): void => {
    const session = hosts.find(host => host.key === key)?.session;
    if (session !== undefined) dispatch(session.newRun.refreshPlaces, () => undefined);
  };

const NO_FOLDERS: Atom.Atom<Readonly<Record<string, string>>> = Atom.make({});

interface ShellViewProps {
  readonly current: Navigation;
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly controls: MachineControls | undefined;
  readonly machineRows: readonly MachineRow[];
  readonly places: readonly PlacesHost[];
  readonly repositories: RepositoriesControls | undefined;
  readonly kept: Readonly<Record<string, string>>;
  readonly look: AppearanceState;
  readonly choose: (appearance: Appearance) => void;
  readonly viewer: Viewer | null;
  readonly issues: Issues;
  readonly setIssues: (update: (all: Issues) => Issues) => void;
  readonly go: (action: NavigationAction) => void;
}

function MachineDestination(props: ShellViewProps): JSX.Element {
  const {current, controls, repositories, go} = props;
  if (current.machines.screen === 'add' && controls !== undefined) {
    return (
      <AddMachinePage
        origin={current.machines.origin}
        controls={controls.add}
        close={() => go({type: 'close-add-machine'})}
        openRuns={() => go({type: 'open-runs'})}
        {...(repositories === undefined
          ? {}
          : {
              saveRepositories: (
                host: string,
                folder: string,
                done: (error: string | null) => void,
              ) => repositories.save(host, folder, done),
            })}
      />
    );
  }
  return (
    <ConnectedMachines
      hosts={props.hosts}
      rows={props.machineRows}
      {...(controls === undefined ? {} : {controls})}
      addMachine={() => go({type: 'open-add-machine', origin: 'machines'})}
      places={{
        hosts: props.places,
        refresh: refreshPlaces(props.hosts, props.dispatch),
      }}
      repositories={props.kept}
    />
  );
}

function SettingsDestination(props: ShellViewProps): JSX.Element {
  return (
    <SettingsScreen
      appearance={props.look}
      choose={props.choose}
      viewer={props.viewer}
      machines={{
        count: props.machineRows.length,
        manage: () => props.go({type: 'open-machines'}),
        ...(props.controls === undefined
          ? {}
          : {add: () => props.go({type: 'open-add-machine', origin: 'machines'})}),
      }}
      diagnostics={{protocolVersion: String(DESKTOP_BUILD.protocolVersion)}}
      {...(props.repositories === undefined ? {} : {repositories: props.repositories})}
    />
  );
}

function RunsDestination(props: ShellViewProps): JSX.Element {
  const {current, hosts, dispatch, go} = props;
  const {runs} = current;
  if (runs.screen === 'run' && runs.view === 'catch-up') {
    return (
      <ConnectedCatchUp
        hosts={hosts}
        route={runs}
        go={go}
        dispatch={dispatch}
        viewerId={props.viewer?.id ?? null}
        words={{role: roleWord, fault: faultWord}}
      />
    );
  }
  return (
    <ConnectedApp
      hosts={hosts}
      dispatch={dispatch}
      route={runs}
      fleet={current.fleet}
      go={go}
      issues={props.issues}
      setIssues={props.setIssues}
      repositories={props.kept}
    />
  );
}

/** The one destination selected by the navigation fold. */
function DestinationScreen(props: ShellViewProps): JSX.Element {
  const {current, hosts, dispatch, go} = props;
  // Add is a child of the flow that opened it. From New run the Runs rail stays active; from the
  // Machines page Machines stays active. The screen itself is shared and does not choose the rail.
  if (current.machines.screen === 'add') return <MachineDestination {...props} />;
  switch (current.destination) {
    case 'runs':
      return <RunsDestination {...props} />;
    case 'cost':
      return (
        <ConnectedCost
          hosts={hosts}
          dispatch={dispatch}
          open={(host, runId) => go({type: 'open-run', host, runId})}
        />
      );
    case 'machines':
      return <MachineDestination {...props} />;
    case 'settings':
      return <SettingsDestination {...props} />;
  }
}

export function ConnectedShell(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly navigation: NavigationStore;
  readonly appearance: AppearanceStore;
  /** This install as the hosts know it (`core/viewer`); null when the main process did not say. */
  readonly viewer: Viewer | null;
  /** Adding and removing machines; a window without it lists them only. */
  readonly machines?: MachineActions;
  /** The Repositories folder of each machine; a window without it shows no such setting. */
  readonly folders?: MachineFolders;
}): JSX.Element {
  const {hosts, dispatch, navigation, appearance, viewer, folders} = props;
  const [issues, setIssues] = useState<Issues>(NO_ISSUES);
  useAtomMount(useMemo(() => watchRuns(hosts), [hosts]));
  const current = useAtomValue(navigation.state);
  const look = useAtomValue(appearance.state);
  const machineRows = useAtomValue(useMemo(() => machinesOf(hosts), [hosts]));
  const choose = (next: Appearance): void => dispatch(appearance.choose(next), () => undefined);
  const controls = useMachineControls(props.machines, dispatch);
  const kept = useAtomValue(folders?.state ?? NO_FOLDERS);
  const places = useAtomValue(useMemo(() => placesOf(hosts), [hosts]));
  const repositories = repositoryControls(folders, machineRows, kept, dispatch);
  return (
    <FocusMemory place={placeOf(current)}>
      {note => {
        const go = (action: NavigationAction): void => {
          // What opens a page keeps the control that opened it, so Escape finds the focus there again.
          note(action);
          // The Machines page shows the places as the hosts last probed them.
          if (action.type === 'open-machines')
            for (const {session} of hosts) dispatch(session.newRun.loadPlaces, () => undefined);
          dispatch(navigation.go(action), () => undefined);
        };
        return (
          <AppShell destination={current.destination} go={go}>
            <DestinationScreen
              current={current}
              hosts={hosts}
              dispatch={dispatch}
              controls={controls}
              machineRows={machineRows}
              places={places}
              repositories={repositories}
              kept={kept}
              look={look}
              choose={choose}
              viewer={viewer}
              issues={issues}
              setIssues={setIssues}
              go={go}
            />
          </AppShell>
        );
      }}
    </FocusMemory>
  );
}
