/**
 * How a gateway connection recovers, and how long each phase of a dial may take. A dial is a
 * sequence of phases, each with the bound that fits what it waits for:
 *
 * - bringing the ssh link up and each short command: a wall bound, because there is no partial
 *   result to measure;
 * - the copy of the host program: measured by progress. Each chunk must be accepted within
 *   `COPY_STALL_MS`, so a slow copy that keeps moving is never cut and a stalled one is, however
 *   large the program grows;
 * - a request with one reply once the program runs, the handshake included: `UNARY_BOUND_MS`.
 *
 * There is no bound on a whole dial, because one would cut a slow copy for being slow. Whether an
 * open session is alive is a separate question (`liveness.ts`). Pure numbers and decisions; the
 * shell that dials applies them.
 */

/** The longest bringing the ssh link up may take (ssh bounds each hop's connect by itself). */
export const ESTABLISH_BOUND_MS = 60_000;
/** The longest a short command on the host (a platform probe, a checksum, an install step) may take. */
export const COMMAND_BOUND_MS = 30_000;
/** The longest the copy may wait for one chunk to be accepted before it counts as stalled. */
export const COPY_STALL_MS = 20_000;
/** The longest a request with one reply may take once the program runs. */
export const UNARY_BOUND_MS = 15_000;

/** Dials in one recovery, the first included. */
export const MAX_DIALS = 5;

const BACKOFF_BASE_MS = 250;
const BACKOFF_CAP_MS = 4_000;

/** gRPC statuses a redial cannot change: the schema disagrees, or the gateway broke its contract. */
const FATAL_CODES: ReadonlySet<number> = new Set([9, 13, 15]);

export type Verdict =
  | {readonly _tag: 'redial'; readonly afterMs: number}
  | {readonly _tag: 'offline'};

/**
 * What to do after the `failures`-th failed dial in a row (1 for the first), which ended with gRPC
 * status `code` when it had one.
 */
export function afterFailure(failures: number, code?: number): Verdict {
  if (code !== undefined && FATAL_CODES.has(code)) return {_tag: 'offline'};
  if (failures >= MAX_DIALS) return {_tag: 'offline'};
  return {_tag: 'redial', afterMs: Math.min(BACKOFF_BASE_MS * 2 ** failures, BACKOFF_CAP_MS)};
}
