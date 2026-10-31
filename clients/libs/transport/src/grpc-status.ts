/** gRPC status codes are the one typed failure vocabulary from the gateway to every screen. */

/** The codes clients branch on. Other codes still travel as their number. */
export const GrpcCode = Object.freeze({
  DEADLINE_EXCEEDED: 4,
  NOT_FOUND: 5,
  FAILED_PRECONDITION: 9,
  UNIMPLEMENTED: 12,
  INTERNAL: 13,
  DATA_LOSS: 15,
} as const);

export class GrpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message);
    this.name = 'GrpcError';
  }
}

/** The gateway's status for a failed call, or undefined when the failure is not a gRPC status. */
export function grpcCode(error: unknown): number | undefined {
  return error instanceof GrpcError ? error.code : undefined;
}

/** The host answered and holds no such run or stream: final, never a lost connection. */
export function isNotFound(error: unknown): boolean {
  return grpcCode(error) === GrpcCode.NOT_FOUND;
}
