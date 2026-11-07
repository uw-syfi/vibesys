import {expect, it} from '@effect/vitest';
import {DesktopRefusal} from '@vibesys/proto';
import * as Schema from 'effect/Schema';
import {
  type AppError,
  CallEnded,
  fromEnd,
  fromTransportError,
  isRetryable,
  NoBridge,
  RpcStatus,
  rpcStatusOf,
  StreamOverflow,
} from './errors.js';

/** Every status a gateway can send, a few it cannot, and none. */
const status = Schema.UndefinedOr(Schema.Int.check(Schema.isBetween({minimum: -1, maximum: 40})));

/** What the status table says each end becomes. */
const expectedTag = (code: number | undefined): AppError['_tag'] => {
  switch (code) {
    case undefined:
      return 'TransportFailed';
    case RpcStatus.UNAVAILABLE:
      return 'Unavailable';
    case RpcStatus.DEADLINE_EXCEEDED:
      return 'DeadlineExceeded';
    case RpcStatus.NOT_FOUND:
      return 'NotFound';
    case RpcStatus.UNIMPLEMENTED:
      return 'Unimplemented';
    case RpcStatus.DATA_LOSS:
      return 'InvalidMessage';
    default:
      return 'Rejected';
  }
};

const RETRYABLE: ReadonlySet<AppError['_tag']> = new Set([
  'TransportFailed',
  'Unavailable',
  'DeadlineExceeded',
  'FellBehind',
]);

const endOf = (message: string, code: number | undefined): CallEnded =>
  new CallEnded({message, ...(code === undefined ? {} : {status: code})});

it.prop(
  'every end is exactly one error whose retryability follows the table',
  {code: status, message: Schema.String},
  ({code, message}) => {
    const error = fromEnd(endOf(message, code));
    expect(error._tag).toBe(expectedTag(code));
    expect(isRetryable(error)).toBe(RETRYABLE.has(error._tag));
  },
);

it.prop(
  'an end keeps the daemon reason as detail',
  {message: Schema.NonEmptyString},
  ({message}) => {
    const error = fromEnd(endOf(message, RpcStatus.INTERNAL));
    expect(error._tag === 'Rejected' && error.detail).toBe(message);
  },
);

it.prop('a refusal reports a real gRPC status', {code: status}, ({code}) => {
  const error = fromEnd(endOf('x', code));
  if (error._tag === 'Rejected') expect(Object.values(RpcStatus)).toContain(error.status);
});

it('an unknown status code is reported as UNKNOWN', () => {
  expect(rpcStatusOf(99)).toBe(RpcStatus.UNKNOWN);
  expect(rpcStatusOf(-1)).toBe(RpcStatus.UNKNOWN);
  expect(rpcStatusOf(14)).toBe(RpcStatus.UNAVAILABLE);
});

it('every transport failure becomes an application error', () => {
  expect(fromTransportError(new NoBridge())._tag).toBe('NoBridge');
  expect(fromTransportError(new StreamOverflow({capacity: 4}))._tag).toBe('FellBehind');
  expect(fromTransportError(endOf('', undefined))._tag).toBe('TransportFailed');
});

it('a bad frame names the response type it could not read', () => {
  const error = fromEnd(endOf('bad frame', RpcStatus.DATA_LOSS), 'pkg.Reply');
  expect(error._tag === 'InvalidMessage' && error.type).toBe('pkg.Reply');
});

const APP_REFUSALS = [
  DesktopRefusal.METHOD_NOT_AVAILABLE,
  DesktopRefusal.INVALID_REQUEST,
  DesktopRefusal.UNSUPPORTED_PLATFORM,
  DesktopRefusal.HOST_PROGRAM_MISSING,
] as const;

it.prop(
  'an end the app refused is a refusal with its cause and its words, never retried, whatever else the end says',
  {
    refusal: Schema.Literals(APP_REFUSALS),
    message: Schema.NonEmptyString,
    code: status,
  },
  ({refusal, message, code}) => {
    const error = fromEnd(
      new CallEnded({message, refusal, ...(code === undefined ? {} : {status: code})}),
    );
    expect(error._tag).toBe('Refused');
    expect(isRetryable(error)).toBe(false);
    if (error._tag === 'Refused') expect(error.detail).toBe(message);
  },
);
