import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {HostCallRequestSchema} from '@vibesys/proto';
import {configuredHostCall} from '@vibesys/runs/intents';

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
