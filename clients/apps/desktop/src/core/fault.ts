/** The screens' view of an `AppError`: a kind from the closed list in `state`, and its detail. */
import type {Fault} from '../state/index.js';
import {type AppError, isRetryable} from './errors.js';

const detailOf = (error: AppError): string => {
  switch (error._tag) {
    case 'NoBridge':
      return 'this window has no connection to the app';
    case 'TransportFailed':
      return error.message;
    case 'AuthenticationFailed':
    case 'Unavailable':
    case 'NotFound':
    case 'Unimplemented':
      return error.detail;
    case 'DeadlineExceeded':
      return error.origin === 'client'
        ? 'the host did not answer in time'
        : 'the host gave up waiting for the call';
    case 'FellBehind':
      return `the stream fell ${error.capacity} frames behind`;
    case 'InvalidMessage':
      return `${error.type}: ${error.detail}`;
    case 'Rejected':
      return error.detail;
  }
};

/** What a screen shows of `error`. A new `AppError` tag fails to compile here. */
export const faultOf = (error: AppError): Fault => ({
  kind: error._tag,
  detail: detailOf(error),
  retryable: isRetryable(error),
});
