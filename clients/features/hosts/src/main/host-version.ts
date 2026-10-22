/**
 * Whether a v2 host's VibeSys checkout can serve this app, asked once when its connection becomes
 * ready (the handshake). The gateway is shipped by the app and always matches it; the checkout is
 * the host's own and can be older. The answer is the registry's own typed one: `inspect-candidate`
 * reports the checkout too old, or the registry answers UNSUPPORTED (the checkout predates the
 * command, or speaks another API schema). Nothing is read from stderr.
 */
import {isHostUnsupported, type RegistrySetup} from '@vibesys/connection';
import type {HostId} from '@vibesys/hosts';

/** The selection revision of a check no user selection owns. */
const NO_SELECTION = 1n;

export type HostVersion = 'current' | 'out-of-date';

/** `current` or `out-of-date`; any other failure (the host unreachable) rejects: it is no verdict. */
export async function checkHostVersion(
  setup: RegistrySetup,
  host: HostId,
  checkout: string,
  signal: AbortSignal,
): Promise<HostVersion> {
  try {
    const inspected = await setup.inspectCandidate(host, checkout, NO_SELECTION, signal);
    return inspected.state === 'too_old' ? 'out-of-date' : 'current';
  } catch (error) {
    if (isHostUnsupported(error)) return 'out-of-date';
    throw error;
  }
}
