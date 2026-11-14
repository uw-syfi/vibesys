/**
 * The Machines page bound to the hosts' services: while it shows, every host's `machine_stats`
 * stream is followed (leaving the page closes the streams and forgets what was read); the one
 * status column and the eval places are the parent's, computed whichever page shows so the
 * Repositories setting and the New run flow can read them too.
 */
import {useAtomMount, useAtomValue} from '@effect/atom-react';
import * as Atom from 'effect/reactivity/Atom';
import {type JSX, useMemo} from 'react';
import {
  type FleetOverview,
  hostFleetOverview,
  type MachineRow,
  machineView,
} from '../../state/index.js';
import type {MachineControls} from '../machines/add-machine/machine-controls.js';
import {type MachineStatsHost, MachinesScreen, type PlacesHost} from '../machines/index.js';
import type {ConnectedHost} from '../orchestrator/connected.js';

/** The `machine_stats` stream of every host, followed while this is mounted. */
const watchMachines = (hosts: readonly ConnectedHost[]): Atom.Atom<void> =>
  Atom.make(get => {
    for (const host of hosts) get(host.session.machine.watch);
  });

/** Each host's CPU, memory and GPU readings as the Machines page shows them. */
const statsOf = (hosts: readonly ConnectedHost[]): Atom.Atom<readonly MachineStatsHost[]> =>
  Atom.make(get =>
    hosts.map(({key, label, session}) => ({
      key,
      label,
      view: machineView(get(session.connection.state), get(session.machine.model)),
    })),
  );

/** What each host's runs are doing now, by host key, for the load on its machine's card. */
const loadOf = (
  hosts: readonly ConnectedHost[],
): Atom.Atom<Readonly<Record<string, FleetOverview>>> =>
  Atom.make(get =>
    Object.fromEntries(
      hosts.map(({key, session}) => [key, hostFleetOverview(get(session.model).data)] as const),
    ),
  );

export function ConnectedMachines(props: {
  readonly hosts: readonly ConnectedHost[];
  readonly rows: readonly MachineRow[];
  /** The Add machine and remove controls; a window without them lists the machines only. */
  readonly controls?: MachineControls;
  readonly addMachine?: () => void;
  readonly places: {
    readonly hosts: readonly PlacesHost[];
    readonly refresh: (key: string) => void;
  };
  /** The explicit repositories folder of each host, by host key. */
  readonly repositories: Readonly<Record<string, string>>;
}): JSX.Element {
  const {hosts, controls} = props;
  useAtomMount(useMemo(() => watchMachines(hosts), [hosts]));
  const stats = useAtomValue(useMemo(() => statsOf(hosts), [hosts]));
  const load = useAtomValue(useMemo(() => loadOf(hosts), [hosts]));
  return (
    <MachinesScreen
      rows={props.rows}
      stats={stats}
      load={load}
      places={props.places}
      repositories={props.repositories}
      {...(props.addMachine === undefined ? {} : {addMachine: props.addMachine})}
      {...(controls === undefined ? {} : {controls})}
    />
  );
}
