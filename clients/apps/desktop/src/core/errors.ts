/**
 * Every way a call to a host can fail, as one closed set. A tag is one reaction by the client:
 * retry, resubscribe, stop and say so, or show the daemon's reason. The transport fails with the
 * three `TransportError`s, and `fromTransportError` turns them into an `AppError`.
 */
import {DesktopLinkProblem, DesktopRefusal} from '@vibesys/proto';
import * as Data from 'effect/Data';
import type * as Duration from 'effect/Duration';
import type {RefusalCause} from '../state/index.js';

/** The gRPC status codes, by name (https://grpc.github.io/grpc/core/md_doc_statuscodes.html). */
export const RpcStatus = {
  OK: 0,
  CANCELLED: 1,
  UNKNOWN: 2,
  INVALID_ARGUMENT: 3,
  DEADLINE_EXCEEDED: 4,
  NOT_FOUND: 5,
  ALREADY_EXISTS: 6,
  PERMISSION_DENIED: 7,
  RESOURCE_EXHAUSTED: 8,
  FAILED_PRECONDITION: 9,
  ABORTED: 10,
  OUT_OF_RANGE: 11,
  UNIMPLEMENTED: 12,
  INTERNAL: 13,
  UNAVAILABLE: 14,
  DATA_LOSS: 15,
  UNAUTHENTICATED: 16,
} as const;
export type RpcStatus = (typeof RpcStatus)[keyof typeof RpcStatus];

const RPC_STATUSES: ReadonlySet<number> = new Set(Object.values(RpcStatus));

/** A status the daemon sent as itself, or `UNKNOWN` for a number that is no gRPC status. */
export const rpcStatusOf = (code: number): RpcStatus =>
  RPC_STATUSES.has(code) ? (code as RpcStatus) : RpcStatus.UNKNOWN;

// Transport failures: what a `GatewayTransport` fails with.

/**
 * A call the gateway or the link ended with a failure. `status` is the gRPC code when there was
 * one; without it, the port or the main process failed, or the call was cancelled.
 */
export class CallEnded extends Data.TaggedError('CallEnded')<{
  readonly message: string;
  readonly status?: number;
  readonly linkProblem?: DesktopLinkProblem;
  /** The app refused the call for good (no host program for the platform, a method it forbids). */
  readonly refusal?: DesktopRefusal;
}> {}

/** A stream held more unread frames than its capacity. */
export class StreamOverflow extends Data.TaggedError('StreamOverflow')<{
  readonly capacity: number;
}> {}

/** The window has no gateway: the code is not running inside the app. */
export class NoBridge extends Data.TaggedError('NoBridge') {}

export type TransportError = CallEnded | StreamOverflow | NoBridge;

// The client's errors: what a call through `makeClient` fails with.

/** SSH key authentication was refused. */
export class AuthenticationFailed extends Data.TaggedError('AuthenticationFailed')<{
  readonly detail: string;
}> {}

/** The end had no gRPC status, or a stream was closed by the daemon. Retry. */
export class TransportFailed extends Data.TaggedError('TransportFailed')<{
  readonly message: string;
}> {}

/** Status 14: the daemon cannot serve now, or the link was lost. Retry. */
export class Unavailable extends Data.TaggedError('Unavailable')<{
  readonly detail: string;
}> {}

/**
 * The client's timer ran out, or the gateway's did (status 4, which does not say how long it
 * waited). Reads retry; a command stays unconfirmed.
 */
export class DeadlineExceeded extends Data.TaggedError('DeadlineExceeded')<{
  readonly origin: 'client' | 'gateway';
  readonly waited?: Duration.Duration;
}> {}

/** A stream fell behind its reader and was cut. Resubscribe from the cursor. */
export class FellBehind extends Data.TaggedError('FellBehind')<{
  readonly capacity: number;
}> {}

/**
 * A message broke its contract, going out (`request`: nothing was sent) or coming in (`response`:
 * the frame was dropped). `type` is the message's full name and `detail` names the field.
 */
export class InvalidMessage extends Data.TaggedError('InvalidMessage')<{
  readonly direction: 'request' | 'response';
  readonly type: string;
  readonly detail: string;
}> {}

/** Status 5. Final: never a lost connection. */
export class NotFound extends Data.TaggedError('NotFound')<{
  readonly detail: string;
}> {}

/** Status 12: the daemon on the host is too old to serve this. */
export class Unimplemented extends Data.TaggedError('Unimplemented')<{
  readonly detail: string;
}> {}

/** Every other status: the daemon refused the call for a reason a retry does not change. */
export class Rejected extends Data.TaggedError('Rejected')<{
  readonly status: RpcStatus;
  readonly detail: string;
}> {}

/**
 * The app or the host refused for a reason that no retry and no redial changes: a platform with no
 * host program, a state directory on a network disk. `cause` is the type the screens name the fix
 * by; `detail` is the refusing side's own words (platform, version, path).
 */
export class Refused extends Data.TaggedError('Refused')<{
  readonly cause: RefusalCause;
  readonly detail: string;
}> {}

export type AppError =
  | AuthenticationFailed
  | NoBridge
  | TransportFailed
  | Unavailable
  | DeadlineExceeded
  | FellBehind
  | InvalidMessage
  | NotFound
  | Unimplemented
  | Rejected
  | Refused;

/** Whether trying the same call again can succeed. A new `AppError` tag fails to compile here. */
export const isRetryable = (error: AppError): boolean => {
  switch (error._tag) {
    case 'TransportFailed':
    case 'Unavailable':
    case 'DeadlineExceeded':
    case 'FellBehind':
      return true;
    case 'AuthenticationFailed':
    case 'NoBridge':
    case 'InvalidMessage':
    case 'NotFound':
    case 'Unimplemented':
    case 'Rejected':
    case 'Refused':
      return false;
  }
};

/** What each refusal of the app is, as the cause the screens name. */
const APP_REFUSAL: Readonly<
  Record<Exclude<DesktopRefusal, DesktopRefusal.UNSPECIFIED>, RefusalCause>
> = {
  [DesktopRefusal.METHOD_NOT_AVAILABLE]: 'method-not-available',
  [DesktopRefusal.INVALID_REQUEST]: 'invalid-request',
  [DesktopRefusal.UNSUPPORTED_PLATFORM]: 'unsupported-platform',
  [DesktopRefusal.HOST_PROGRAM_MISSING]: 'host-program-missing',
};

const detailOf = (end: CallEnded): string =>
  end.message === '' ? 'the connection ended' : end.message;

/**
 * The client's error for a call that ended. `type` is the response message's name, for the one
 * status (15) that says the gateway could not read a frame.
 */
export const fromEnd = (end: CallEnded, type = '(unknown)'): AppError => {
  if (end.linkProblem === DesktopLinkProblem.AUTH)
    return new AuthenticationFailed({detail: detailOf(end)});
  if (end.refusal !== undefined && end.refusal !== DesktopRefusal.UNSPECIFIED)
    return new Refused({cause: APP_REFUSAL[end.refusal], detail: detailOf(end)});
  if (end.status === undefined) return new TransportFailed({message: detailOf(end)});
  const detail = detailOf(end);
  switch (end.status) {
    case RpcStatus.UNAVAILABLE:
      return new Unavailable({detail});
    case RpcStatus.DEADLINE_EXCEEDED:
      return new DeadlineExceeded({origin: 'gateway'});
    case RpcStatus.NOT_FOUND:
      return new NotFound({detail});
    case RpcStatus.UNIMPLEMENTED:
      return new Unimplemented({detail});
    case RpcStatus.DATA_LOSS:
      return new InvalidMessage({direction: 'response', type, detail});
    default:
      return new Rejected({status: rpcStatusOf(end.status), detail});
  }
};

/** The client's error for any failure of the transport. */
export const fromTransportError = (error: TransportError, type?: string): AppError => {
  switch (error._tag) {
    case 'CallEnded':
      return fromEnd(error, type);
    case 'StreamOverflow':
      return new FellBehind({capacity: error.capacity});
    case 'NoBridge':
      return error;
  }
};
