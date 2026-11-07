/**
 * The ways the main process's gateway work fails, as a closed set. `GrpcFailed` carries the
 * status a gateway or its peer answered with, which the window shows as a typed refusal;
 * `GatewayRefused` is the app's own refusal of a call that no retry changes (a platform no host
 * program is built for); `GatewayFailed` is every other end (the link, the provisioning, the dial,
 * a closed bridge), which a redial may fix.
 */
import type {DesktopRefusal} from '@vibesys/proto';
import * as Data from 'effect/Data';

/** A call the gateway ended with a gRPC status other than OK. */
export class GrpcFailed extends Data.TaggedError('GrpcFailed')<{
  readonly code: number;
  readonly message: string;
}> {}

/**
 * The app refused a call for good: the method is not one it allows, or it has no host program for
 * the host's platform. A retry or a redial cannot change either, so nothing retries it, and the
 * window is told the `refusal` as a type and `message` as the words (platform, version).
 */
export class GatewayRefused extends Data.TaggedError('GatewayRefused')<{
  readonly refusal: DesktopRefusal;
  readonly message: string;
}> {}

/** A gateway session, a dial, a link or a provisioning step that did not work. */
export class GatewayFailed extends Data.TaggedError('GatewayFailed')<{
  readonly message: string;
}> {}

/** Why an ssh link could not be brought up or used. */
export type LinkProblem = 'auth' | 'link' | 'capacity';

/** The ssh link to a host failed; `problem` says whether the user, the network or the host did. */
export class LinkFailed extends Data.TaggedError('LinkFailed')<{
  readonly problem: LinkProblem;
  readonly message: string;
}> {}

/** A process could not be started. */
export class SpawnFailed extends Data.TaggedError('SpawnFailed')<{
  readonly command: string;
  readonly message: string;
}> {}

/** The gRPC status codes the gateway code names; every other code travels as its number. */
export const GrpcCode = {
  DEADLINE_EXCEEDED: 4,
  RESOURCE_EXHAUSTED: 8,
  FAILED_PRECONDITION: 9,
  UNIMPLEMENTED: 12,
  INTERNAL: 13,
  UNAVAILABLE: 14,
  DATA_LOSS: 15,
} as const;

/** The status a failure ended its call with, when it is a gRPC status. */
export const grpcCodeOf = (error: unknown): number | undefined =>
  error instanceof GrpcFailed ? error.code : undefined;

/** The one line a failure reads as in the log and in the window's end frame. */
export const failureMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);
