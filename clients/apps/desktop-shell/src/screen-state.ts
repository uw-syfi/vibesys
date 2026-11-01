/**
 * Whether each screen of the shell has read what it shows: the one place that says, for every
 * screen, which replies it waits for. A screen carries the answer as `data-state` on its page
 * (`loading` or `ready`) and the rail carries its own, so a test or a script waits for a state
 * instead of a duration: `ready` means no reply the screen asked for is outstanding, so nothing
 * it reads can still change what it shows. An answer that failed has answered: the screen shows
 * that problem instead of waiting for it. A screen over more than one store is told by the shell,
 * which owns the stores; a screen that reads nothing says `ready` itself.
 */
import {type SidebarState, sidebarAwaiting} from '@vibesys/feature-app-shell/state';
import {type MachinesState, machinesAwaiting} from '@vibesys/feature-machines/state';
import {isAwaiting, type NewRunSnapshot} from '@vibesys/feature-new-run/state';
import {
  type MachineSettingsState,
  machineSettingsAwaiting,
  type SettingsState,
  settingsAwaiting,
} from '@vibesys/feature-settings/state';
import {type UsageScreenState, usageAwaiting} from '@vibesys/feature-usage/state';
import type {HostKey} from '@vibesys/hosts';
import {type PageState, pageState} from '@vibesys/ui';

/**
 * The runs the shell lists and the machines it knows: ready once the overview, the saved machines
 * and each project's runs have answered. The Runs dashboard draws them, and the rail's mark goes
 * to Runs once a run is known and to New run when none is, so both say it.
 */
export function runsSettled(
  sidebar: SidebarState,
  machines: Pick<MachinesState, 'listing'>,
): PageState {
  return pageState(sidebarAwaiting(sidebar) || machines.listing !== null);
}

/** Machines: ready once the machines are listed and each one's check, save and test has answered. */
export function machinesSettled(machines: MachinesState): PageState {
  return pageState(machinesAwaiting(machines));
}

/** Usage: ready once the figures are read (a resting phase) and the Limits question is answered. */
export function usageSettled(usage: UsageScreenState): PageState {
  return pageState(usageAwaiting(usage));
}

/**
 * Settings: ready once the preferences are read (and any change saved), the machines are listed
 * and checked, and the machine the sections are about has been read.
 */
export function settingsSettled(
  settings: SettingsState,
  machines: MachinesState,
  machineSettings: MachineSettingsState,
): PageState {
  return pageState(
    settingsAwaiting(settings) ||
      machinesAwaiting(machines) ||
      machineSettingsAwaiting(machineSettings),
  );
}

/**
 * Whether every read the shell started for New run, on `machine`, has answered: the runs list the
 * form follows (the machine of the run opened last), the machines the form offers, and the
 * Settings data of the machine it is for (what it has set up to measure on).
 */
function newRunDataRead(
  sidebar: Pick<SidebarState, 'overview'>,
  machines: Pick<MachinesState, 'listing'>,
  machineSettings: Pick<MachineSettingsState, 'views'>,
  machine: HostKey,
): boolean {
  const compute = machineSettings.views[machine]?.compute;
  return (
    sidebar.overview.kind !== 'waiting' &&
    machines.listing === null &&
    compute !== undefined &&
    compute.kind !== 'loading'
  );
}

/** New run: ready once the shell's reads for it and the screen's own (what its machine offers) have. */
export function newRunSettled(
  sidebar: Pick<SidebarState, 'overview'>,
  machines: Pick<MachinesState, 'listing'>,
  machineSettings: Pick<MachineSettingsState, 'views'>,
  snapshot: NewRunSnapshot,
): PageState {
  const read = newRunDataRead(sidebar, machines, machineSettings, snapshot.context.form.machine);
  return pageState(!read || isAwaiting(snapshot));
}
