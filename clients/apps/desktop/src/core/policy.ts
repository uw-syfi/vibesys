/** The timings and limits the services run by, in one place. */
import type * as Duration from 'effect/Duration';

/** How long a Hello may take before the attempt counts as failed. */
export const CONNECT_DEADLINE: Duration.Input = '15 seconds';
/** The longest a command waits for its receipt (main ends a unary call at the same age). */
export const COMMAND_DEADLINE: Duration.Input = '15 seconds';

/** The wait before redialing: exponential from `baseMs`, never above `capMs`, jittered down by half. */
export const RECONNECT = {baseMs: 500, capMs: 30_000} as const;
/** A Ready period this long earns the whole schedule again; a link that flaps faster never does. */
export const STABLE_AFTER_MS = 30_000;

/**
 * A pass that applied this many frames in order, the opening read included, was healthy: the
 * restart that ends it (a gap, a reader that fell behind, a clean close) is not held against the
 * feed. A pass that ends sooner is quiet.
 */
export const STABLE_PASS_FRAMES = 4;
/** Quiet restarts of one feed in a row before it blames the connection, whose backoff spaces the next open. */
export const MAX_QUIET_RESTARTS = 3;
/** How many of an agent's newest items its first page holds. */
export const AGENT_PAGE_SIZE = 40;
