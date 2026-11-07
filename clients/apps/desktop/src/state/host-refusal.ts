/**
 * Why a host that answers cannot run a run, as a refusal the app shows and does not retry. The host
 * says it itself (`Identify` answers whether or not it serves) and types it: a state directory it
 * refused, for a reason about where it is, a configuration it cannot read, or a part a run needs (the
 * program that holds an agent session, the skills, the job tool) that is missing or older than the
 * host needs. A host that is merely held by another process, or whose holder has gone, is not
 * refusing for good: it is left to the reconnect policy.
 */
import {
  HostPart,
  type IdentifyResponse,
  oneofIs,
  PartProblemReason,
  type PartReadiness,
  StateRefusal,
  type Unserved,
  UnservedReason,
} from '@vibesys/orchestrator-proto';
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

const PART_CAUSE: Readonly<
  Record<
    Exclude<HostPart, HostPart.UNSPECIFIED>,
    readonly [missing: RefusalCause, old: RefusalCause]
  >
> = {
  [HostPart.SESSION_HOLDER]: ['session-holder-missing', 'session-holder-too-old'],
  [HostPart.SKILLS]: ['skills-missing', 'skills-too-old'],
  [HostPart.JOB_TOOL]: ['job-tool-missing', 'job-tool-too-old'],
};

/** The first part of a host that a run cannot be started with, in the host's own words. */
export function partRefusalOf(parts: readonly PartReadiness[]): HostRefusal | undefined {
  for (const {part, state} of parts) {
    if (part === HostPart.UNSPECIFIED || !oneofIs(state, 'problem', {ready: true, problem: true})) {
      continue;
    }
    const [missing, old] = PART_CAUSE[part];
    const {reason, detail} = state.value;
    return {cause: reason === PartProblemReason.TOO_OLD ? old : missing, detail};
  }
  return undefined;
}

/**
 * Everything a host says that stops a run from starting: it does not serve the orchestrator, or one
 * of its parts is missing or too old. One answer to `Identify` and one table of causes, so the next
 * thing a host lacks is caught the way these are.
 */
export function readinessOf(id: IdentifyResponse): HostRefusal | undefined {
  const {orchestrator} = id;
  const unserved = oneofIs(orchestrator, 'unserved', {serving: true, unserved: true})
    ? refusalOf(orchestrator.value)
    : undefined;
  return unserved ?? partRefusalOf(id.parts);
}
