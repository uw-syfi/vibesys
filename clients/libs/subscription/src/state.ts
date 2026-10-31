/**
 * What a screen can know about a subscription, as one closed type. Every non-`failed` state is
 * bounded by a timer or by a bounded layer below it, so no state means "loading with no end", and
 * a value exists only in `live` and `stale`, so a screen cannot show data it does not hold.
 */

/** Milliseconds since the epoch on the clock the subscription's owner injects. Display only. */
export type Instant = number;

/** How far a stream has come toward its first frame, in the order it reaches them. */
export const CONNECT_STEPS = ['gateway', 'handshake', 'stream'] as const;
export type ConnectStep = (typeof CONNECT_STEPS)[number];

/** Why a stream was lost or refused and another may follow. The text of an error decides nothing. */
export const RETRYABLE_REASONS = [
  'unreachable',
  'ended',
  'silent',
  'outdated',
  'no_first_frame',
  'source_failed',
  'resource_exhausted',
  'internal',
] as const;
/** Why no retry follows: the host cannot produce the topic, a frame broke the protocol, or this side closed. */
export const TERMINAL_REASONS = ['unsupported', 'invalid_frame', 'closed'] as const;

export type StaleReason = (typeof RETRYABLE_REASONS)[number];
export type FailureReason = StaleReason | (typeof TERMINAL_REASONS)[number];

/** The state without its value: what main's machine decides and sends to every window. */
export type Control =
  | {readonly kind: 'connecting'; readonly step: ConnectStep; readonly attempt: number}
  | {readonly kind: 'live'; readonly since: Instant}
  | {readonly kind: 'stale'; readonly reason: StaleReason; readonly attempt: number}
  | {readonly kind: 'failed'; readonly reason: FailureReason; readonly retrying: boolean};

export type SubscriptionState<S> =
  | {readonly kind: 'connecting'; readonly step: ConnectStep; readonly attempt: number}
  | {readonly kind: 'live'; readonly value: S; readonly since: Instant}
  | {
      readonly kind: 'stale';
      readonly value: S;
      readonly reason: StaleReason;
      readonly attempt: number;
    }
  | {readonly kind: 'failed'; readonly reason: FailureReason; readonly retrying: boolean};

/** Whether a reason leaves another attempt to come. */
export function retryable(reason: FailureReason): reason is StaleReason {
  return !(TERMINAL_REASONS as readonly string[]).includes(reason);
}

/** What a subscription says before anything has happened. */
export const CONNECTING: Extract<Control, {readonly kind: 'connecting'}> = {
  kind: 'connecting',
  step: CONNECT_STEPS[0],
  attempt: 0,
};

/** The failures after which a screen is told the host is failing, not recovering. */
export const OFFLINE_AFTER = 5;
