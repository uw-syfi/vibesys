/**
 * What the shell reads for New run, and whether it has all been answered: New run's own machine
 * reads what its machine offers (`isAwaiting`), and the shell reads the rest, which the form
 * follows: the runs list (the machine of the run opened last), the machines the form offers, and
 * the Settings data of the machine it is for (what it has set up to measure on).
 */
import type {SidebarState} from '@vibesys/feature-app-shell/state';
import type {MachinesState} from '@vibesys/feature-machines/state';
import type {MachineSettingsState} from '@vibesys/feature-settings/state';
import type {HostKey} from '@vibesys/hosts';

/**
 * Whether every read the shell started for New run, on `machine`, has answered. An answer that
 * failed has answered: the screen shows that problem instead of waiting for it.
 */
export function newRunDataRead(
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
