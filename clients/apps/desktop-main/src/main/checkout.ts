import type {HostId} from '@vibesys/hosts';
import {hostKey} from '@vibesys/hosts';
import type {SettingsStore} from '@vibesys/settings';

/**
 * The checkout a host runs VibeSys from: the one the user saved, else this app's own for the local
 * machine. Every native call carries it, so a fresh profile with no saved entry works.
 */
export function effectiveCheckout(
  settings: SettingsStore,
  localCheckout: string,
): (id: HostId) => Promise<string | null> {
  return async id =>
    (await settings.hosts()).settings.hosts[hostKey(id)]?.checkout ??
    (id.kind === 'local' ? localCheckout : null);
}
