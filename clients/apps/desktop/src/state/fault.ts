/**
 * What went wrong with a call, as the screens read it. `core/errors.ts` owns the typed errors and
 * projects each onto a `Fault` (`failureOf`); this folder cannot import `effect`, so it owns the
 * list of kinds and `core` is checked against it (`satisfies Record<FaultKind, ...>`).
 */

/** One kind per client reaction: the tag of each error the client distinguishes. */
export const FAULT_KINDS = [
  'AuthenticationFailed',
  'NoBridge',
  'TransportFailed',
  'Unavailable',
  'DeadlineExceeded',
  'FellBehind',
  'InvalidMessage',
  'NotFound',
  'Unimplemented',
  'Rejected',
  'Refused',
] as const;

export type FaultKind = (typeof FAULT_KINDS)[number];

/**
 * Why a host or the app refused for good: no retry changes it, so the screens say which cause and
 * what to change. The first five are the app's own refusals (a method, a request, a platform no
 * host program is built for, a build that holds none for it, a host that lacks what installing it
 * needs); the rest are the host's, typed from
 * what it says about why it does not serve.
 */
export const REFUSAL_CAUSES = [
  'method-not-available',
  'invalid-request',
  'unsupported-platform',
  'host-program-missing',
  'host-prerequisite-missing',
  'state-network-filesystem',
  'state-unknown-filesystem',
  'state-refused',
  'config-refused',
] as const;

export type RefusalCause = (typeof REFUSAL_CAUSES)[number];

export interface Fault {
  readonly kind: FaultKind;
  /** What the host or the transport said, shown verbatim as "Details" to the one who can fix it. */
  readonly detail: string;
  /** Trying the same call again may work; false means the call can never work as sent. */
  readonly retryable: boolean;
  /** For a `Refused` fault, the cause. */
  readonly cause?: RefusalCause;
}
