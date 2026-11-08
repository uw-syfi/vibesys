import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {oneofIs} from '@vibesys/orchestrator-proto';
import {DesktopGatewayKind, DesktopGatewayRequestSchema} from '@vibesys/proto';
import fc from 'fast-check';
import {
  decodeGatewayReply,
  encodeGatewayRequest,
  gatewayEnd,
  gatewayReply,
  parseGatewayRequest,
} from './gateway-codec.js';

const MAX_ERROR_BYTES = 65536;

const REQUEST = {
  kind: 'stream' as const,
  host: 'ssh:host-a',
  method: '/vibesys.orchestrator.v1.OrchestratorService/Subscribe',
  bytes: new Uint8Array([1, 2]),
};

const request = fc.record({
  kind: fc.constantFrom('unary' as const, 'stream' as const),
  host: fc.constantFrom('local', 'ssh:host-a', 'ssh:b.c@d'),
  method: fc.constantFrom('/a.B/C', '/vibesys.api.v1.HostService/Hello'),
  bytes: fc.uint8Array({maxLength: 64}),
});

const encodedWith = (change: Record<string, unknown>): Uint8Array =>
  toBinary(
    DesktopGatewayRequestSchema,
    create(DesktopGatewayRequestSchema, {
      kind: DesktopGatewayKind.STREAM,
      host: REQUEST.host,
      method: REQUEST.method,
      bytes: REQUEST.bytes,
      ...change,
    }),
  );

describe('the gateway codec', () => {
  it('reads back any request it wrote', () => {
    fc.assert(
      fc.property(request, value => {
        expect(parseGatewayRequest(encodeGatewayRequest(value))).toEqual(value);
      }),
    );
  });

  it('refuses what is not a request the window may send', () => {
    expect(() => parseGatewayRequest(REQUEST)).toThrow('protobuf bytes');
    expect(() => encodeGatewayRequest({...REQUEST, host: '-oProxyCommand=x'})).toThrow();
    for (const change of [
      {kind: DesktopGatewayKind.UNSPECIFIED},
      {kind: 99},
      {host: '-oProxyCommand=x'},
      {method: 'Tail'},
      {bytes: new Uint8Array(16777217)},
    ]) {
      expect(() => parseGatewayRequest(encodedWith(change))).toThrow();
    }
    const encoded = encodeGatewayRequest(REQUEST);
    expect(() => parseGatewayRequest(new Uint8Array([...encoded, 0x28, 1]))).toThrow();
  });

  it('tells a frame from an end, and carries the failure and its status', () => {
    fc.assert(
      fc.property(fc.uint8Array({maxLength: 64}), bytes => {
        expect(decodeGatewayReply(gatewayReply(bytes))).toEqual({case: 'bytes', value: bytes});
      }),
    );
    expect(decodeGatewayReply(gatewayEnd())).toMatchObject({case: 'end', value: {}});
    expect(decodeGatewayReply(gatewayEnd('denied', 7))).toMatchObject({
      case: 'end',
      value: {error: 'denied', status: 7},
    });
    expect(() => decodeGatewayReply(new Uint8Array([255]))).toThrow();
    expect(() => gatewayEnd('x', 17)).toThrow();
  });

  it('bounds a failure message that overruns the end reply wire limit, instead of failing to build it', () => {
    const over = 'x'.repeat(MAX_ERROR_BYTES + 1);
    expect(() => gatewayEnd(over)).not.toThrow();
    const reply = decodeGatewayReply(gatewayEnd(over));
    if (!oneofIs(reply, 'end', {bytes: true, end: true})) throw new Error('not an end');
    expect(reply.value.error).toBe('x'.repeat(MAX_ERROR_BYTES));
  });

  it('bounds any failure message to the wire limit without splitting the character at the cut', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('a', 'é', '中', '😀'),
        fc.integer({min: MAX_ERROR_BYTES - 8, max: MAX_ERROR_BYTES + 8}),
        (unit, length) => {
          const message = unit.repeat(length);
          const reply = decodeGatewayReply(gatewayEnd(message));
          if (!oneofIs(reply, 'end', {bytes: true, end: true})) throw new Error('not an end');
          const error = reply.value.error ?? '';
          const messageBytes = new TextEncoder().encode(message).byteLength;
          const errorBytes = new TextEncoder().encode(error).byteLength;
          expect(errorBytes).toBeLessThanOrEqual(MAX_ERROR_BYTES);
          if (messageBytes <= MAX_ERROR_BYTES) expect(error).toBe(message);
          else expect(message.startsWith(error)).toBe(true);
        },
      ),
    );
  });
});
