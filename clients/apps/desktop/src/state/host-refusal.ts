/**
 * Why a host that answers does not serve the orchestrator, as a refusal the app shows and does not
 * retry. The host says it itself (`Identify` answers whether or not it serves) and types it: a
 * state directory it refused, for a reason about where it is, or a configuration it cannot read.
 * A host that is merely held by another process, or whose holder has gone, is not refusing for
 * good: it is left to the reconnect policy.
 */
import {StateRefusal, type Unserved, UnservedReason} from '@vibesys/orchestrator-proto';
import type {RefusalCause} from './fault.js';

/** What the host refuses, and in its own words. */
export interface HostRefusal {
  readonly cause: RefusalCause;
  readonly detail: string;
}

const stateCause = (why: StateRefusal): RefusalCause => {
  switch (why) {
    case StateRefusal.NETWORK_FILESYSTEM:
      return 'state-network-filesystem';
    case StateRefusal.UNKNOWN_FILESYSTEM:
      return 'state-unknown-filesystem';
    case StateRefusal.UNSPECIFIED:
      return 'state-refused';
  }
};

/** The refusal an `Unserved` says, or undefined when waiting may fix it. */
export function refusalOf(unserved: Unserved): HostRefusal | undefined {
  const detail = unserved.detail;
  switch (unserved.reason) {
    case UnservedReason.STATE_REFUSED:
      return {cause: stateCause(unserved.stateRefusal), detail};
    case UnservedReason.CONFIG_REFUSED:
      return {cause: 'config-refused', detail};
    case UnservedReason.HELD_ELSEWHERE:
    case UnservedReason.HOLDER_GONE:
    case UnservedReason.UNSPECIFIED:
      return undefined;
  }
}
