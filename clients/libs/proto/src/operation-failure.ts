import {FailureCategory} from './generated/vibesys/host/v1/failure_pb.js';

/** What every client says of a host whose VibeSys lacks an operation; the host's own text is dropped. */
export const HOST_UNSUPPORTED_MESSAGE = "This host's VibeSys needs an update";

/**
 * A typed registry failure. Callers branch on category, never on diagnostic text. An unsupported
 * operation (the host's checkout predates it) carries `HOST_UNSUPPORTED_MESSAGE` and nothing the
 * host wrote, so no screen can show that host's usage dump.
 */
export class OperationFailed extends Error {
  readonly category: number;
  constructor(category: number, message: string) {
    super(category === FailureCategory.UNSUPPORTED ? HOST_UNSUPPORTED_MESSAGE : message);
    this.name = 'OperationFailed';
    this.category = category;
  }

  /** Whether the host's VibeSys does not provide the operation. */
  get unsupported(): boolean {
    return this.category === FailureCategory.UNSUPPORTED;
  }
}
