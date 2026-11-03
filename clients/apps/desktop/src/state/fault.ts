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
] as const;

export type FaultKind = (typeof FAULT_KINDS)[number];

export interface Fault {
  readonly kind: FaultKind;
  /** What the host or the transport said, shown verbatim as "Details" to the one who can fix it. */
  readonly detail: string;
  /** Trying the same call again may work; false means the call can never work as sent. */
  readonly retryable: boolean;
}
