import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  FailureCategory,
  HostCallResponseSchema,
  MutateResponseSchema,
  ReadResponseSchema,
  WatchResponseSchema,
} from '@vibesys/proto';
import {HOST_CALL_METHOD, MUTATE_METHOD, READ_METHOD, WATCH_METHOD} from './gateway.js';
import {failureDiagnostic} from './gateway-failures.js';

const failure = {category: FailureCategory.INTERNAL, message: 'vibesys host list-compute exited 2'};

test('a failed registry reply gives the text the host wrote, for every method kind', () => {
  const replies: [string, Uint8Array][] = [
    [
      READ_METHOD,
      toBinary(
        ReadResponseSchema,
        create(ReadResponseSchema, {result: {case: 'failure', value: failure}}),
      ),
    ],
    [
      MUTATE_METHOD,
      toBinary(
        MutateResponseSchema,
        create(MutateResponseSchema, {result: {case: 'failure', value: failure}}),
      ),
    ],
    [
      WATCH_METHOD,
      toBinary(
        WatchResponseSchema,
        create(WatchResponseSchema, {result: {case: 'failure', value: failure}}),
      ),
    ],
  ];
  for (const [method, bytes] of replies) {
    expect(failureDiagnostic(method, bytes)).toContain(failure.message);
  }
  const refused = toBinary(
    HostCallResponseSchema,
    create(HostCallResponseSchema, {
      result: {
        case: 'configurationFailed',
        value: {code: 'host_command_failed', message: 'exited 2', stderr: 'usage: vibesys host'},
      },
    }),
  );
  expect(failureDiagnostic(HOST_CALL_METHOD, refused)).toBe(
    'host_command_failed: exited 2\nusage: vibesys host',
  );
});

test('a reply that did not fail, an unknown method and undecodable bytes give nothing', () => {
  const quiet = toBinary(ReadResponseSchema, create(ReadResponseSchema, {}));
  expect(failureDiagnostic(READ_METHOD, quiet)).toBeNull();
  expect(failureDiagnostic('/other.Service/Method', quiet)).toBeNull();
  expect(failureDiagnostic(READ_METHOD, new Uint8Array([255, 255]))).toBeNull();
  expect(
    failureDiagnostic(
      HOST_CALL_METHOD,
      toBinary(HostCallResponseSchema, create(HostCallResponseSchema, {})),
    ),
  ).toBeNull();
});
