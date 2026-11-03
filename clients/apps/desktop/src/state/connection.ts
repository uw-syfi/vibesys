/**
 * Where the link to one host stands. `core/connection.ts` is the only writer. Ready means the
 * orchestrator's Hello was answered COMPATIBLE, not that a socket is open.
 */
import type {BuildVersions} from '@vibesys/orchestrator-proto';
import type {Fault} from './fault.js';

export type HostConnectionState =
  /** Nothing asked for the host yet. */
  | {readonly _tag: 'Idle'}
  /** Dialing, with the orchestrator Hello in flight. */
  | {readonly _tag: 'Connecting'; readonly attempt: number}
  /** Hello answered COMPATIBLE; `epoch` counts the Ready periods of this connection. */
  | {readonly _tag: 'Ready'; readonly epoch: number; readonly daemon: BuildVersions}
  /** Between attempts; `retryAtMs` is the Clock time of the next one. */
  | {
      readonly _tag: 'Waiting';
      readonly attempt: number;
      readonly fault: Fault;
      readonly retryAtMs: number;
    }
  /** The two builds cannot talk. Terminal until the user retries. */
  | {
      readonly _tag: 'Incompatible';
      readonly side: 'client-too-old' | 'daemon-too-old';
      readonly daemon?: BuildVersions;
    }
  /** No bridge, or the daemon rejects the call outright. Terminal until the user retries. */
  | {readonly _tag: 'Unusable'; readonly fault: Fault};

export const idleConnection: HostConnectionState = {_tag: 'Idle'};

/** Whether the connection can no longer succeed without the user choosing to retry. */
export function isTerminal(
  state: HostConnectionState,
): state is Extract<HostConnectionState, {_tag: 'Incompatible' | 'Unusable'}> {
  return state._tag === 'Incompatible' || state._tag === 'Unusable';
}
