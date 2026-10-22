import {HOST_UNSUPPORTED_MESSAGE, OperationFailed} from '@vibesys/proto';
import {HelperRequestError} from './helper-channel.js';

/**
 * Whether `error` says the host's VibeSys cannot do what was asked: the helper did not negotiate
 * the feature, or the registry answered UNSUPPORTED (its checkout lacks the version handshake or speaks another schema). Callers
 * answer their screen's `unsupported` reply or `needs-update` status instead of the host's text.
 */
export function isHostUnsupported(error: unknown): boolean {
  if (error instanceof OperationFailed) return error.unsupported;
  return error instanceof HelperRequestError && error.code === 'feature_not_negotiated';
}

export {HOST_UNSUPPORTED_MESSAGE};
