import {FailureCategory} from './generated/vibesys/host/v1/failure_pb.js';

/** What every screen says of a host whose VibeSys lacks an operation. */
export const HOST_UNSUPPORTED_MESSAGE = "This host's VibeSys needs an update";

/**
 * A typed registry failure. Callers branch on category, never on text. Its message is fixed, one
 * sentence per kind of failure that no host text can change, so a screen that shows an error's
 * message never shows what the host or gateway wrote (stderr, an argparse usage dump). That text
 * is `diagnostic`, for logs.
 */
export class OperationFailed extends Error {
  readonly category: number;
  readonly diagnostic: string;
  constructor(category: number, diagnostic: string) {
    super(category === FailureCategory.UNSUPPORTED ? HOST_UNSUPPORTED_MESSAGE : 'host operation failed');
    this.name = 'OperationFailed';
    this.category = category;
    this.diagnostic = diagnostic;
  }

  /** Whether the host's VibeSys does not provide the operation. */
  get unsupported(): boolean {
    return this.category === FailureCategory.UNSUPPORTED;
  }
}
