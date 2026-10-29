import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {decodeGatewayReply} from '@vibesys/ipc';
import {HostCallRequestSchema} from '@vibesys/proto';
import {configuredHostCall} from '@vibesys/runs/intents';
import {GrpcError} from '@vibesys/transport';
import {endOf} from './gateway-ipc.js';

test('HostCall uses the current host configuration and preserves its typed method', () => {
  const request = create(HostCallRequestSchema, {
    checkout: '/renderer-supplied',
    method: {
      case: 'postIntent',
      value: {
        projectRoot: '/project',
        runId: 'run-1',
        requestId: 'r-1',
        intent: {case: 'pause', value: {}},
      },
    },
  });
  for (const checkout of ['/src/vibesys', '~/another checkout']) {
    const configured = fromBinary(
      HostCallRequestSchema,
      configuredHostCall(toBinary(HostCallRequestSchema, request), checkout),
    );
    expect(configured.checkout).toBe(checkout);
    expect(configured.method).toEqual(request.method);
  }
});

test('HostCall refuses missing host configuration and malformed protobuf', () => {
  expect(() => configuredHostCall(new Uint8Array(), null)).toThrow('Set a checkout first');
  expect(() => configuredHostCall(new Uint8Array([255]), '/src/vibesys')).toThrow();
});

test('terminal replies keep every gRPC status and a single error prefix', () => {
  const ended = (error?: unknown) => {
    const reply = decodeGatewayReply(endOf(error));
    if (reply.case !== 'end') throw new Error('not an end');
    return {error: reply.value.error, status: reply.value.status};
  };
  for (let code = 0; code <= 16; code++)
    expect(ended(new GrpcError(code, 'missing run'))).toEqual({error: 'missing run', status: code});
  expect(ended(new Error('caller cancelled'))).toEqual({
    error: 'Error: caller cancelled',
    status: undefined,
  });
  expect(ended()).toEqual({error: undefined, status: undefined});
});
