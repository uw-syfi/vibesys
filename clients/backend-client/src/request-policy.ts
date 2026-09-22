import type {RequestInput} from './protocol.js';

/**
 * Per-request-type policy, kept as data so both this client and a caller's own
 * double-submit guard (#840) read idempotency from one table instead of each
 * re-deciding it. A policy answers three questions the send path asks of every
 * request: is re-issuing it safe, does it need its own connection, and how long
 * to wait for an answer.
 */
export interface RequestPolicy {
  /**
   * Whether issuing the request twice has the same effect as issuing it once.
   * Only an idempotent request is auto-resent after a control-channel drop: a
   * `command.pause` the server may or may not have applied is safe to repeat,
   * whereas a `command.steer` or a `query.chat` submission is not, so those
   * fail rather than risk a double effect. `#840` reads the same flag to decide
   * which buttons a double click may safely re-fire.
   */
  readonly idempotent: boolean;
  /**
   * Whether the request runs on its own connection with no response deadline.
   * A `query.chat` is bounded by the agent it drives, not by the control RPC
   * timeout, and a long one must not block pause/resume/snapshot in the
   * server's per-connection loop, so it gets a dedicated socket.
   */
  readonly dedicatedConnection: boolean;
  /**
   * Response deadline in ms, or `undefined` to use the client-wide default. A
   * dedicated-connection request carries no deadline regardless of this field.
   */
  readonly timeoutMs?: number;
}

/**
 * A read, or any request not named below. Reads are idempotent: re-issuing a
 * `query.snapshot` returns the current snapshot with no side effect, so a read
 * outstanding at a drop is safe to resend on the recovered channel.
 */
export const DEFAULT_REQUEST_POLICY: RequestPolicy = {
  idempotent: true,
  dedicatedConnection: false,
};

/**
 * The requests whose policy differs from the read default. Mutations that
 * create or append (`query.chat`, `query.chat_thread_create`, `command.steer`)
 * are not idempotent, so a resend would double the effect; `command.pause` and
 * `command.resume` set a bit and are safe to repeat. `query.chat` also runs on
 * its own connection (see `RequestPolicy.dedicatedConnection`).
 */
export const REQUEST_POLICIES: Readonly<Record<string, RequestPolicy>> = {
  'query.chat': {idempotent: false, dedicatedConnection: true},
  'query.chat_thread_create': {idempotent: false, dedicatedConnection: false},
  'command.steer': {idempotent: false, dedicatedConnection: false},
  'command.pause': {idempotent: true, dedicatedConnection: false},
  'command.resume': {idempotent: true, dedicatedConnection: false},
};

/**
 * The subset of `AbortSignal` this client uses, declared structurally rather
 * than by referencing the global. The neutral packages that re-export
 * `RequestOptions` (core-state) compile without DOM or node lib types in scope,
 * so the signal type must not depend on either; a real `AbortSignal` satisfies
 * this interface.
 */
export interface AbortSignalLike {
  readonly aborted: boolean;
  readonly reason?: unknown;
  addEventListener(type: 'abort', listener: () => void, options?: {once?: boolean}): void;
  removeEventListener(type: 'abort', listener: () => void): void;
}

/** Per-call overrides layered onto a request type's table policy. */
export interface RequestOptions {
  /** Override the response deadline in ms for this one call. */
  timeoutMs?: number;
  /** Force this one call onto its own connection with no response deadline. */
  dedicatedConnection?: boolean;
  /**
   * Abort the request. An abort before it is sent stops it from being sent; an
   * abort while it is outstanding frees its slot and rejects the promise, and
   * its request id is never reused, so a late response cannot be misrouted to a
   * later request.
   */
  signal?: AbortSignalLike;
}

/**
 * The effective policy for one call: the type's table entry with the caller's
 * overrides applied. Idempotency is a property of the operation, not the call,
 * so it is not overridable; the connection shape and the deadline are.
 */
export function resolveRequestPolicy(
  type: RequestInput['type'],
  options: RequestOptions = {},
): RequestPolicy {
  const base = (type === undefined ? undefined : REQUEST_POLICIES[type]) ?? DEFAULT_REQUEST_POLICY;
  const timeoutMs = options.timeoutMs ?? base.timeoutMs;
  return {
    idempotent: base.idempotent,
    dedicatedConnection: options.dedicatedConnection ?? base.dedicatedConnection,
    // Omitted rather than set to undefined, so the client falls back to its own
    // default deadline; `exactOptionalPropertyTypes` forbids the explicit field.
    ...(timeoutMs === undefined ? {} : {timeoutMs}),
  };
}
