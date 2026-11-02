/**
 * What went wrong with a connection or a request, and how long to wait before dialling again.
 * Shared by the host machine and the machine of each open agent.
 */
import {SourceError} from '@vibesys/orchestrator-source';
import type {BackoffSchedule} from '@vibesys/transport';

/** Why a connection or a request did not work, as the screens word it. */
export interface Fault {
  /** `ended` the host closed the stream; `timeout` it did not answer in time. */
  readonly kind: 'lost' | 'refused' | 'invalid' | 'ended' | 'timeout';
  /** What the host or the transport said, for the one who can fix it. */
  readonly detail: string;
}

export function faultOf(error: unknown): Fault {
  if (error instanceof SourceError) return {kind: error.kind, detail: error.message};
  return {kind: 'lost', detail: error instanceof Error ? error.message : String(error)};
}

export const timeout = (what: string): Fault => ({
  kind: 'timeout',
  detail: `${what} did not answer in time`,
});

/** How long a new connection has to give its first answer. */
export const CONNECT_DEADLINE_MS = 15_000;
/** How long a request (older steps, ideas, a message) has to be answered. */
export const REQUEST_DEADLINE_MS = 20_000;
/** A connection that has kept up this long has earned the whole schedule again next time. */
export const STABLE_AFTER_MS = 30_000;

/** A fresh cursor over the shared redial schedule (`BackoffSchedule` of `@vibesys/transport`). */
export type NewBackoff = () => BackoffSchedule;

/**
 * The wait before redial number `attempts` (0 is the first), or `undefined` once the shared
 * schedule is spent: the machine then stops redialling and leaves the failure standing with a
 * Retry. The schedule is a cursor, so the machine keeps only the count (context stays plain data)
 * and replays a fresh cursor to that position. The composition injects the schedule because a
 * feature's state imports no transport code, only its types.
 */
export function redialDelay(newBackoff: NewBackoff, attempts: number): number | undefined {
  const schedule = newBackoff();
  for (let skipped = 0; skipped < attempts; skipped++) schedule.next();
  return schedule.next();
}
