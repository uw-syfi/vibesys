/**
 * How a gateway connection recovers: a bounded number of dials with growing waits between them,
 * inside one budget, and a few statuses that end the attempt at once because dialing again cannot
 * change them. Pure numbers and decisions; the shell that dials applies them.
 */

/** A dial runs the host's provisioning (an artifact upload and three commands) and a handshake. */
const COMMAND_BOUND_MS = 30_000;
const UPLOAD_BOUND_MS = 90_000;
/** The longest one dial may take. */
export const DIAL_DEADLINE_MS = UPLOAD_BOUND_MS + 3 * COMMAND_BOUND_MS;

/** The longest a first connection may take: it may have to upload the gateway. */
const FIRST_CONNECT_BUDGET_MS = 6 * 60_000;
/** The longest a reconnect may take: the host already holds the gateway. */
const RECONNECT_BUDGET_MS = 2 * 60_000;

/** Dials in one recovery, the first included. */
export const MAX_DIALS = 5;

const BACKOFF_BASE_MS = 250;
const BACKOFF_CAP_MS = 4_000;

/** gRPC statuses a redial cannot change: the schema disagrees, or the gateway broke its contract. */
const FATAL_CODES: ReadonlySet<number> = new Set([9, 13, 15]);

export const connectBudgetMs = (provisioned: boolean): number =>
  provisioned ? RECONNECT_BUDGET_MS : FIRST_CONNECT_BUDGET_MS;

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
