/**
 * A call the gateway answered with a gRPC status rejects with an error whose numeric `status` is
 * that code. Faults below the protocol (a dead link, a cancelled call) carry none.
 */
const STATUS = {
  invalidArgument: 3,
  notFound: 5,
  permissionDenied: 7,
  resourceExhausted: 8,
  failedPrecondition: 9,
  outOfRange: 11,
  unimplemented: 12,
  dataLoss: 15,
  unauthenticated: 16,
} as const;

/** Why the gateway will never serve this run; retrying the same request cannot change it. */
export type LoadRefusal =
  | 'not-found'
  | 'data-loss'
  | 'too-large'
  | 'unsupported'
  | 'invalid'
  | 'denied';

const REFUSALS: Readonly<Record<number, LoadRefusal>> = {
  [STATUS.notFound]: 'not-found',
  [STATUS.dataLoss]: 'data-loss',
  [STATUS.resourceExhausted]: 'too-large',
  [STATUS.unimplemented]: 'unsupported',
  [STATUS.invalidArgument]: 'invalid',
  [STATUS.failedPrecondition]: 'invalid',
  [STATUS.outOfRange]: 'invalid',
  [STATUS.permissionDenied]: 'denied',
  [STATUS.unauthenticated]: 'denied',
};

export function refusalOf(error: unknown): LoadRefusal | undefined {
  if (typeof error !== 'object' || error === null || !('status' in error)) return undefined;
  const {status} = error;
  return typeof status === 'number' && Object.hasOwn(REFUSALS, status)
    ? REFUSALS[status]
    : undefined;
}
