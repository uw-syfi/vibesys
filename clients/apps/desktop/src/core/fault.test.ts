import {describe, expect, it} from '@effect/vitest';
import {FAULT_KINDS} from '../state/index.js';
import {
  type AppError,
  AuthenticationFailed,
  DeadlineExceeded,
  FellBehind,
  InvalidMessage,
  isRetryable,
  NoBridge,
  NotFound,
  Rejected,
  RpcStatus,
  TransportFailed,
  Unavailable,
  Unimplemented,
} from './errors.js';
import {faultOf} from './fault.js';

/** One of every error the app's calls can fail with. */
const everyError: ReadonlyArray<AppError> = [
  new NoBridge(),
  new AuthenticationFailed({detail: 'key refused'}),
  new TransportFailed({message: 'port closed'}),
  new Unavailable({detail: 'link lost'}),
  new DeadlineExceeded({origin: 'client'}),
  new FellBehind({capacity: 2048}),
  new InvalidMessage({direction: 'response', type: 'Receipt', detail: 'bad'}),
  new NotFound({detail: 'agent'}),
  new Unimplemented({detail: 'method'}),
  new Rejected({status: RpcStatus.PERMISSION_DENIED, detail: 'no'}),
];

describe('the fault a screen sees', () => {
  it('has one kind per error, and the kinds are exactly the closed list', () => {
    expect(everyError.map(error => faultOf(error).kind).sort()).toEqual([...FAULT_KINDS].sort());
  });

  it('keeps the retryable answer of the error and says something about it', () => {
    for (const error of everyError) {
      const fault = faultOf(error);
      expect(fault.retryable).toBe(isRetryable(error));
      expect(fault.detail).not.toBe('');
    }
  });
});
