import type {HostConnection} from '../shell/host-connection.js';

/** Resolve once `connection` reaches a phase of `kind` (now or later). */
export function phaseReached(connection: HostConnection, kind: string): Promise<void> {
  if (connection.phase.kind === kind) return Promise.resolve();
  return new Promise(resolve => {
    const stop = connection.onPhase(phase => {
      if (phase.kind !== kind) return;
      stop();
      resolve();
    });
  });
}
