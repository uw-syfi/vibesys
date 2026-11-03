/**
 * The wait between connection attempts. Pure arithmetic over a count of consecutive failures and a
 * unit of jitter, so a property test covers every count without a clock; `reconnectDelay` draws the
 * jitter from the ambient `Random`.
 */
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import {RECONNECT, STABLE_AFTER_MS} from './policy.js';

export interface ReconnectPolicy {
  readonly baseMs: number;
  readonly capMs: number;
  readonly stableAfterMs: number;
}

export const defaultReconnect: ReconnectPolicy = {...RECONNECT, stableAfterMs: STABLE_AFTER_MS};

/** The longest wait after `failures` (at least 1) consecutive failures. */
export const ceilingMs = (policy: ReconnectPolicy, failures: number): number =>
  Math.min(policy.capMs, policy.baseMs * 2 ** (failures - 1));

/** The wait after `failures` consecutive failures: between half the ceiling and the ceiling. */
export const delayMs = (policy: ReconnectPolicy, failures: number, unit: number): number => {
  const ceiling = ceilingMs(policy, failures);
  return Math.floor(ceiling * (0.5 + 0.5 * unit));
};

/**
 * The failure count after an attempt fails. `readyForMs` is how long the connection was Ready
 * before it was lost, or null when it never was. Only a Ready period of `stableAfterMs` or more
 * starts the count over, so a link that comes up and drops quickly keeps backing off.
 */
export const failuresAfter = (
  policy: ReconnectPolicy,
  failures: number,
  readyForMs: number | null,
): number => (readyForMs !== null && readyForMs >= policy.stableAfterMs ? 1 : failures + 1);

/** The jittered wait after `failures` consecutive failures. */
export const reconnectDelay = (
  policy: ReconnectPolicy,
  failures: number,
): Effect.Effect<Duration.Duration> =>
  Effect.map(Random.next, unit => Duration.millis(delayMs(policy, failures, unit)));
