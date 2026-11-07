/**
 * The window bound to the app's services: the rail, and the screen of the destination it shows.
 * The run streams of every host are followed for as long as the window is open, whichever screen
 * shows, so the Resources tab reads live connections; the navigation and the appearance are the
 * stores the entry built, and a screen's callbacks become the intents those stores take.
 */
import {useAtomMount, useAtomValue} from '@effect/atom-react';
import type {Appearance} from '@vibesys/ui/tokens';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo, useState} from 'react';
import type {AppearanceStore, NavigationStore} from '../../core/index.js';
import {type NavigationAction, type ResourceRow, resourceRow} from '../../state/index.js';
import {
  ConnectedApp,
  type ConnectedHost,
  type Dispatch,
  type Issues,
  NO_ISSUES,
} from '../orchestrator/connected.js';
import {faultWord, roleWord} from '../orchestrator/words.js';
import {SettingsScreen} from '../settings/settings-screen.js';
import {ConnectedCatchUp} from './connected-catch-up.js';
import {AppShell} from './shell.js';

/** The run streams of every host, followed while this is mounted. */
const watchRuns = (hosts: readonly ConnectedHost[]): Atom.Atom<void> =>
  Atom.make(get => {
    for (const host of hosts) get(host.session.runs.watch);
  });

/** Each host as the Resources tab shows it. */
const resourcesOf = (hosts: readonly ConnectedHost[]): Atom.Atom<readonly ResourceRow[]> =>
  Atom.make(get =>
    hosts.map(({key, label, session}) =>
      resourceRow({
        key,
        label,
        connection: get(session.connection.state),
        runsFeed: get(session.runs.status),
        hasData: get(session.model).data.sequence > 0n,
      }),
    ),
  );

export function ConnectedShell(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly dispatch: Dispatch;
  readonly navigation: NavigationStore;
  readonly appearance: AppearanceStore;
  /** The id of this install as the hosts know it (`core/viewer`); null when the main process did not say. */
  readonly viewerId: string | null;
}): JSX.Element {
  const {hosts, dispatch, navigation, appearance, viewerId} = props;
  const [issues, setIssues] = useState<Issues>(NO_ISSUES);
  useAtomMount(useMemo(() => watchRuns(hosts), [hosts]));
  const current = useAtomValue(navigation.state);
  const look = useAtomValue(appearance.state);
  const resources = useAtomValue(useMemo(() => resourcesOf(hosts), [hosts]));
  const go = (action: NavigationAction): void => dispatch(navigation.go(action), () => undefined);
  const choose = (next: Appearance): void => dispatch(appearance.choose(next), () => undefined);
  const {runs} = current;
  /** The run whose catch-up page shows, when it does: it has a screen of its own over the run's views. */
  const catchUp =
    current.destination === 'runs' && runs.screen === 'run' && runs.view === 'catch-up'
      ? runs
      : null;
  return (
    <AppShell destination={current.destination} go={go}>
      {catchUp !== null ? (
        <ConnectedCatchUp
          key="catch-up"
          hosts={hosts}
          route={catchUp}
          go={go}
          dispatch={dispatch}
          viewerId={viewerId}
          words={{role: roleWord, fault: faultWord}}
        />
      ) : current.destination === 'runs' ? (
        <ConnectedApp
          key="runs"
          hosts={hosts}
          dispatch={dispatch}
          route={runs}
          go={go}
          issues={issues}
          setIssues={setIssues}
        />
      ) : (
        <SettingsScreen
          key="settings"
          tab={current.settingsTab}
          pick={tab => go({type: 'pick-settings-tab', tab})}
          appearance={look}
          choose={choose}
          resources={resources}
        />
      )}
    </AppShell>
  );
}
