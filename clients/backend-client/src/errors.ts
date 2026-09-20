import type {Diagnostic} from './protocol.js';

/**
 * What a failure means, independent of the transport that produced it:
 *
 * - `rejected`: the server received the request and refused it. Retrying the
 *   same request gets the same answer, so a capability probe treats this, and
 *   only this, as the server not supporting the probed field.
 * - `timeout`: a deadline elapsed before the server answered.
 * - `parse`: the peer sent bytes this protocol cannot read: malformed JSON, an
 *   unknown message type, or an unsupported protocol version.
 * - `disconnected`: the transport failed to reach the server or lost it. Says
 *   nothing about how the server would have answered.
 *
 * The kinds name outcomes rather than mechanisms (no errno, no socket state),
 * so a second transport can classify its own failures into the same set.
 */
export type BackendErrorKind = 'rejected' | 'timeout' | 'parse' | 'disconnected';

/** Kinds that are transient by nature, so retrying the operation may succeed. */
const RETRYABLE_KINDS: ReadonlySet<BackendErrorKind> = new Set(['timeout', 'disconnected']);

/**
 * Every rejection out of this package, so callers discriminate on `kind` and
 * `retryable` instead of matching message prose. `retryable` defaults from the
 * kind; a transport that knows better (a dial errno that says the endpoint can
 * never accept) overrides it at construction, which keeps the transport-specific
 * classification at the one site that owns it.
 */
export class BackendClientError extends Error {
  readonly kind: BackendErrorKind;
  readonly retryable: boolean;

  constructor(
    kind: BackendErrorKind,
    message: string,
    options: {retryable?: boolean; cause?: unknown} = {},
  ) {
    super(message, options.cause === undefined ? undefined : {cause: options.cause});
    this.name = 'BackendClientError';
    this.kind = kind;
    this.retryable = options.retryable ?? RETRYABLE_KINDS.has(kind);
  }
}

/** A server refusal, including its optional structured diagnostic. */
export class ServerError extends BackendClientError {
  constructor(
    message: string,
    readonly diagnostic: Diagnostic | null = null,
  ) {
    super('rejected', message);
    this.name = 'ServerError';
  }
}

/**
 * Whether the server itself refused the operation, as opposed to the transport
 * failing to carry it. The capability probes fall back only on this: a
 * transport failure is not a verdict on the probed field.
 */
export function isServerRejection(error: unknown): boolean {
  return error instanceof BackendClientError && error.kind === 'rejected';
}
