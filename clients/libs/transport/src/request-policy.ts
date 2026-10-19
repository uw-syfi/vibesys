import type {RequestInput} from '@vibesys/contracts';

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
 * The policy of a read. Reads are idempotent: re-issuing a
 * `query.snapshot` returns the current snapshot with no side effect, so a read
 * outstanding at a drop is safe to resend on the recovered channel.
 */
export const DEFAULT_REQUEST_POLICY: RequestPolicy = {
  idempotent: true,
  dedicatedConnection: false,
};

const READ = DEFAULT_REQUEST_POLICY;

/**
 * Every request type's policy, keyed by the protocol's own request types, so a new
 * request type does not compile until it is classified here. Mutations that create
 * or append (`query.chat`, `query.chat_thread_create`, `command.steer`) are not
 * idempotent, so a resend would double the effect; `command.pause` and
 * `command.resume` set a bit and are safe to repeat. `command.control` carries a
 * client-chosen `control_id`, but until the server keeps answered ids across
 * reconnects a resend is not proven safe, so it is not idempotent. `query.chat`
 * also runs on its own connection (see `RequestPolicy.dedicatedConnection`).
 */
export const REQUEST_POLICIES: Readonly<Record<RequestInput['type'], RequestPolicy>> = {
  'query.chat': {idempotent: false, dedicatedConnection: true},
  'query.chat_thread_create': {idempotent: false, dedicatedConnection: false},
  'command.steer': {idempotent: false, dedicatedConnection: false},
  'command.control': {idempotent: false, dedicatedConnection: false},
  'command.stop': {idempotent: true, dedicatedConnection: false},
  'command.pause': {idempotent: true, dedicatedConnection: false},
  'command.resume': {idempotent: true, dedicatedConnection: false},
  'query.design': READ,
  'query.design_patch': READ,
  'query.events': READ,
  'query.experiments': READ,
  'query.history': READ,
  'query.iterations': READ,
  'query.performance': READ,
  'query.snapshot': READ,
  subscribe: READ,
};

/**
 * The subset of `AbortSignal` this client uses, declared structurally rather
 * than by referencing the global. The neutral packages that re-export
 * `RequestOptions` compile without DOM or node lib types in scope,
 * so the signal type must not depend on either; a real `AbortSignal` satisfies
 * this interface.
 */
export interface AbortSignalLike {
  readonly aborted: boolean;
  readonly reason?: unknown;
  addEventListener(type: 'abort', listener: () => void, options?: {once?: boolean}): void;
  removeEventListener(type: 'abort', listener: () => void): void;
}

/**
 * The result of atomically attaching an abort listener.
 *
 * `aborted` is true when cancellation won while the listener was being
 * attached. In that case the callback has deliberately not run: the caller
 * still owns its not-yet-published operation and settles it before handing the
 * operation to another owner. `detach` is idempotent and is already effective
 * when `aborted` is true.
 */
export interface AbortRegistration {
  readonly aborted: boolean;
  detach(): void;
}

/**
 * Attach one abort listener without a check/register gap.
 *
 * Structural signal implementations may change `aborted` or even dispatch the
 * callback synchronously from `addEventListener`. That is an ownership
 * handoff, not a normal later notification: latch it, remove whatever the
 * registration installed, and let the caller settle its unpublished work via
 * the returned `aborted` bit. Once registration succeeds, a later abort calls
 * `onAbort` exactly once and detaches the listener on every exit path.
 */
export function registerAbort(signal: AbortSignalLike, onAbort: () => void): AbortRegistration {
  let attached = false;
  let aborted = signal.aborted;
  let registering = true;
  let active = true;
  const detach = (): void => {
    if (!active) return;
    active = false;
    if (!attached) return;
    attached = false;
    try {
      signal.removeEventListener('abort', abort);
    } catch {
      // Logical detachment is the ownership boundary. A hostile structural
      // implementation may retain or throw from physical removal; `active`
      // keeps that retained callback inert, and cleanup must never replace the
      // operation's actual terminal result.
    }
  };
  const abort = (): void => {
    if (!active || aborted) return;
    aborted = true;
    if (registering) return;
    try {
      onAbort();
    } finally {
      detach();
    }
  };
  if (!aborted) {
    // Own a possibly installed listener before crossing the structural
    // boundary. If registration throws after installing it, `detach` still
    // attempts removal and fences the retained callback.
    attached = true;
    try {
      signal.addEventListener('abort', abort, {once: true});
    } catch (error) {
      detach();
      throw error;
    }
    // `abort` may have run while `attached` was still false. It intentionally
    // did not call the external callback during that incomplete handoff.
    const abortedAtHandoff = aborted || signal.aborted;
    registering = false;
    if (abortedAtHandoff) {
      aborted = true;
      detach();
    }
  } else {
    registering = false;
  }
  return {aborted, detach};
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
 * The error an abort rejects with: the signal's reason when it is an `Error`
 * (the caller's own), otherwise a standard `AbortError`. Kept out of the
 * transport-failure taxonomy so a caller-initiated cancel is never mistaken for
 * a disconnect, and kept here, beside the signal it reads, so every transport
 * reports an abandoned request the same way.
 */
export function abortReason(signal: AbortSignalLike): Error {
  const reason = signal.reason;
  if (reason instanceof Error) return reason;
  const error = new Error(typeof reason === 'string' && reason ? reason : 'Request aborted');
  error.name = 'AbortError';
  return error;
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
  const base = REQUEST_POLICIES[type];
  const timeoutMs = options.timeoutMs ?? base.timeoutMs;
  return {
    idempotent: base.idempotent,
    dedicatedConnection: options.dedicatedConnection ?? base.dedicatedConnection,
    // Omitted rather than set to undefined, so the client falls back to its own
    // default deadline; `exactOptionalPropertyTypes` forbids the explicit field.
    ...(timeoutMs === undefined ? {} : {timeoutMs}),
  };
}
