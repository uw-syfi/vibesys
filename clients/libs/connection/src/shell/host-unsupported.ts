import {FailureCategory, OperationFailed} from '@vibesys/proto';
import {HelperRequestError} from './helper-channel.js';

/** What every client says of a host whose VibeSys is too old for the app. */
export const HOST_UNSUPPORTED_MESSAGE = "This host's VibeSys needs an update";

/**
 * Whether `error` says the host's VibeSys cannot do what was asked: the helper did not negotiate
 * the feature, or the gateway typed the registry's answer UNSUPPORTED (its checkout lacks the
 * version handshake or speaks another schema). Callers answer their screen's `unsupported` reply
 * or `needs-update` status instead of the host's text.
 */
export function isHostUnsupported(error: unknown): boolean {
  if (error instanceof OperationFailed) return error.category === FailureCategory.UNSUPPORTED;
  return error instanceof HelperRequestError && error.code === 'feature_not_negotiated';
}
