/** Setup reads that run before a checkout is saved.

Hello stays the gateway schema handshake. These two calls put the candidate
path, or "~" when the prefix is relative, on the envelope. They do not read
the saved checkout and they do not fall back to the helper.
*/
import type {HostId} from '@vibesys/hosts';
import {
  acceptCompletePaths,
  acceptInspectCandidate,
  CandidateState,
  callCompletePaths,
  callInspectCandidate,
  type InspectCandidateResponse,
} from '@vibesys/proto';
import {registryTransport} from './registry-reads.js';

export type InspectedCandidate =
  | {
      readonly state: 'found';
      readonly root: string;
      readonly version: string;
      readonly commit: string | null;
      readonly dirty: boolean | null;
      readonly compatible: boolean;
    }
  | {readonly state: 'not_found'}
  | {readonly state: 'too_old'; readonly version: string; readonly min: string};

export interface CompletedPaths {
  readonly directory: string;
  readonly entries: readonly {
    readonly name: string;
    readonly path: string;
    readonly checkout: boolean;
    readonly project: boolean;
  }[];
  readonly truncated: boolean;
}

export interface RegistrySetup {
  inspectCandidate(
    host: HostId,
    path: string,
    revision: bigint,
    signal: AbortSignal,
  ): Promise<InspectedCandidate>;
  completePaths(host: HostId, prefix: string, signal: AbortSignal): Promise<CompletedPaths>;
}

export interface RegistrySetupPorts {
  unary(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
}

/** An absolute or home-relative path travels as itself. Anything else uses "~". */
function envelopeCheckout(path: string): string {
  if (path.startsWith('/') || path === '~' || path.startsWith('~/')) return path;
  return '~';
}

function projectInspection(response: InspectCandidateResponse): InspectedCandidate {
  switch (response.state) {
    case CandidateState.FOUND: {
      const identity = response.identity;
      if (identity === undefined || identity.root.length === 0 || identity.version.length === 0) {
        throw new Error('inspect candidate returned no identity');
      }
      return {
        state: 'found',
        root: identity.root,
        version: identity.version,
        commit: identity.commit ?? null,
        dirty: identity.dirty ?? null,
        compatible: identity.compatible,
      };
    }
    case CandidateState.NOT_FOUND:
      return {state: 'not_found'};
    case CandidateState.TOO_OLD: {
      const identity = response.identity;
      if (identity === undefined) throw new Error('inspect candidate returned no identity');
      return {state: 'too_old', version: identity.version, min: response.minimumVersion};
    }
    default:
      throw new Error('inspect candidate returned an unknown state');
  }
}

/** InspectCandidate and CompletePaths. Budgets stay in the generated registry. */
export function registrySetup(ports: RegistrySetupPorts): RegistrySetup {
  function transport(host: HostId, checkout: string, signal: AbortSignal) {
    if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
    return registryTransport(checkout, (bytes, owned) => ports.unary(host, bytes, owned));
  }
  return {
    async inspectCandidate(host, path, revision, signal) {
      try {
        const response = await callInspectCandidate(
          transport(host, envelopeCheckout(path), signal),
          acceptInspectCandidate({
            requestId: 'registry-setup',
            path,
            selectionRevision: revision,
          }),
          signal,
        );
        if (response.selectionRevision !== revision) throw new Error('stale selection');
        return projectInspection(response);
      } catch (error) {
        if (error instanceof Error && error.message === 'response correlation mismatch') {
          throw new Error('stale selection', {cause: error});
        }
        throw error;
      }
    },
    async completePaths(host, prefix, signal) {
      const response = await callCompletePaths(
        transport(host, envelopeCheckout(prefix), signal),
        acceptCompletePaths({requestId: 'registry-setup', prefix}),
        signal,
      );
      return {
        directory: response.directory,
        truncated: response.truncated,
        entries: response.entries.map(entry => ({
          name: entry.name,
          path: entry.path,
          checkout: entry.vibesysCheckout,
          project: entry.vibesysProject,
        })),
      };
    },
  };
}
