/**
 * How a gateway connection recovers, and how long each phase of a dial may take. A dial is a
 * sequence of phases, each with the bound that fits what it waits for:
 *
 * - bringing the ssh link up and each short command: a wall bound, because there is no partial
 *   result to measure;
 * - the copy of the host program: measured by progress. Each chunk must be accepted within
 *   `COPY_STALL_MS`, so a slow copy that keeps moving is never cut and a stalled one is, however
 *   large the program grows;
 * - a request once the program runs, the handshake included: two different silences. A link that
 *   delivers nothing at all for a whole `REPLY_STALL_MS` window is silent, and its call is cut:
 *   there is no sign the host is there. A link that keeps delivering (a keepalive, a probe's
 *   answer, the middle of a message) while the call has had no reply of its own is alive and the
 *   host is slow: the call waits for `SLOW_ANSWER_BOUND_MS`, a bound on a host that answers
 *   nothing for this call at all, not on a host that is busy. A stream is held to this until its
 *   first reply only: a quiet feed afterwards is not stalled.
 *
 * There is no bound on a whole dial, because one would cut a slow copy for being slow. Whether an
 * open session is alive is a separate question (`liveness.ts`). A host's connection outlives its
 * last call by `CONNECTION_LINGER_MS`, so a connect that is a handshake followed by a feed dials
 * once. Pure numbers and decisions; the shell that dials applies them.
 */

/** The longest bringing the ssh link up may take (ssh bounds each hop's connect by itself). */
export const ESTABLISH_BOUND_MS = 60_000;
/** The longest a short command on the host (a platform probe, a checksum, an install step) may take. */
export const COMMAND_BOUND_MS = 30_000;
/** The longest the copy may wait for one chunk to be accepted before it counts as stalled. */
export const COPY_STALL_MS = 20_000;
/**
 * The window a call is watched in. A call that heard nothing for a whole window is stalled, so one
 * that stops is cut between one and two windows after its last bytes, and one that delivers
 * something in every window never is.
 */
export const REPLY_STALL_MS = 10_000;
/**
 * The longest a call waits for its first reply (or its end) from a host whose link keeps
 * delivering bytes. A heavily loaded host answers slowly, not never; the bound only ends a host
 * that is alive on the link yet never answers this call.
 */
export const SLOW_ANSWER_BOUND_MS = 300_000;
/** How long a host's connection stays up after its last call ends, for the next call to find it. */
export const CONNECTION_LINGER_MS = 10_000;

/** Dials in one recovery, the first included. */
export const MAX_DIALS = 5;

const BACKOFF_BASE_MS = 250;
const BACKOFF_CAP_MS = 4_000;

/** gRPC statuses a redial cannot change: the schema disagrees, or the gateway broke its contract. */
const FATAL_CODES: ReadonlySet<number> = new Set([9, 13, 15]);

/** What a dial's failure says about whether another dial can differ. */
export interface FailedDial {
  /** The gRPC status it ended with, when it had one. */
  readonly code?: number | undefined;
  /** The app refused the call for a reason no redial changes (a platform with no host program). */
  readonly refused: boolean;
}

/** gRPC status of a call cut by a stalled link or a host that did not answer in time. */
const DEADLINE_EXCEEDED = 4;

export type Verdict =
  | {readonly _tag: 'redial'; readonly afterMs: number}
  | {readonly _tag: 'offline'};

/**
 * What to do after the `failures`-th failed dial in a row (1 for the first). A refusal, and a
 * status a redial cannot change, end it at once; the rest are redialed with backoff.
 */
export function afterFailure(failures: number, failed: FailedDial = {refused: false}): Verdict {
  if (failed.refused) return {_tag: 'offline'};
  if (failed.code !== undefined && FATAL_CODES.has(failed.code)) return {_tag: 'offline'};
  if (failures >= MAX_DIALS) return {_tag: 'offline'};
  // A dial that stalled already waited a whole window: the host was slow, not refusing, and a
  // longer pause would only add to the wait. Only failures that came back at once back off.
  const steps = failed.code === DEADLINE_EXCEEDED ? 1 : failures;
  return {_tag: 'redial', afterMs: Math.min(BACKOFF_BASE_MS * 2 ** steps, BACKOFF_CAP_MS)};
}
