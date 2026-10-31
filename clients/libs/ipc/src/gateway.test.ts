import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {DesktopGatewayKind, DesktopGatewayRequestSchema} from '@vibesys/proto';
import {SUBSCRIBE_METHOD} from '@vibesys/subscription';
import fc from 'fast-check';
import {
  decodeGatewayReply,
  encodeGatewayRequest,
  GatewayError,
  type GatewayPortBridge,
  gatewayBridge,
  gatewayEnd,
  gatewayReply,
  parseGatewayRequest,
} from './index.js';

const REQUEST = {
  kind: 'stream' as const,
  host: 'ssh:host-a',
  method: '/vibesys.api.v1.HostService/Tail',
  bytes: new Uint8Array([1, 2]),
};

class Ports implements GatewayPortBridge {
  readonly calls: Array<{
    request: Uint8Array;
    reply(bytes: Uint8Array): void;
    fail(error: string): void;
    cancelled: number;
  }> = [];
  open(request: Uint8Array, reply: (bytes: Uint8Array) => void, fail: (error: string) => void) {
    parseGatewayRequest(request);
    const call = {request, reply, fail, cancelled: 0};
    this.calls.push(call);
    return {
      cancel: () => {
        call.cancelled += 1;
      },
    };
  }
  last() {
    const call = this.calls.at(-1);
    if (call === undefined) throw new Error('missing call');
    return call;
  }
}

test('gateway routing is validated protobuf, snapshots bytes and rejects unknown keys', () => {
  const encoded = encodeGatewayRequest(REQUEST);
  expect(parseGatewayRequest(encoded)).toEqual(REQUEST);
  expect(() => parseGatewayRequest(REQUEST)).toThrow('protobuf bytes');
  expect(() => encodeGatewayRequest({...REQUEST, extra: true} as typeof REQUEST)).toThrow('extra');
  for (const change of [
    {kind: DesktopGatewayKind.UNSPECIFIED},
    {kind: 99},
    {host: '-oProxyCommand=x'},
    {method: 'Tail'},
    {bytes: new Uint8Array(16777217)},
  ]) {
    expect(() =>
      parseGatewayRequest(
        toBinary(
          DesktopGatewayRequestSchema,
          create(DesktopGatewayRequestSchema, {
            kind: DesktopGatewayKind.STREAM,
            host: REQUEST.host,
            method: REQUEST.method,
            bytes: REQUEST.bytes,
            ...change,
          }),
        ),
      ),
    ).toThrow();
  }
  expect(() => parseGatewayRequest(new Uint8Array([...encoded, 0x28, 1]))).toThrow('#5');
});

test('unary waits for a typed end and retires the port exactly once', async () => {
  const ports = new Ports();
  const bridge = gatewayBridge(ports);
  const call = bridge.unary('local', REQUEST.method, REQUEST.bytes);
  const remote = ports.last();
  expect(parseGatewayRequest(remote.request).kind).toBe('unary');
  remote.reply(gatewayReply(new Uint8Array([3, 4])));
  remote.reply(gatewayEnd());
  expect(await call.result).toEqual(new Uint8Array([3, 4]));
  remote.reply(gatewayEnd('late'));
  call.cancel();
  expect(remote.cancelled).toBe(1);
});

test('unary rejects every malformed or failed ending and retires each port', async () => {
  for (const ending of [
    'missing',
    'duplicate',
    'error',
    'malformed',
    'delivery',
    'cancel',
  ] as const) {
    const ports = new Ports();
    const call = gatewayBridge(ports).unary('local', REQUEST.method, REQUEST.bytes);
    const rejected = call.result.catch((error: unknown) => error);
    const remote = ports.last();
    if (ending === 'duplicate') {
      remote.reply(gatewayReply(REQUEST.bytes));
      remote.reply(gatewayReply(REQUEST.bytes));
    } else if (ending === 'error') remote.reply(gatewayEnd('denied'));
    else if (ending === 'malformed') remote.reply(new Uint8Array([255]));
    else if (ending === 'delivery') remote.fail('delivery failed');
    else if (ending === 'cancel') call.cancel();
    else remote.reply(gatewayEnd());
    expect(await rejected).toBeInstanceOf(Error);
    expect(remote.cancelled).toBe(1);
  }
});

test('stream data, failure and cancellation use protobuf and ignore late delivery', () => {
  for (const outcome of ['end', 'cancel', 'consumer'] as const) {
    const ports = new Ports();
    const received: Uint8Array[] = [];
    const ended: Array<string | undefined> = [];
    const call = gatewayBridge(ports).stream(
      'local',
      REQUEST.method,
      REQUEST.bytes,
      bytes => {
        if (outcome === 'consumer') throw new Error('consumer failed');
        received.push(bytes);
      },
      error => ended.push(error),
    );
    const remote = ports.last();
    remote.reply(gatewayReply(REQUEST.bytes));
    if (outcome === 'cancel') call.cancel();
    else remote.reply(gatewayEnd(outcome === 'end' ? 'denied' : undefined));
    remote.reply(gatewayReply(REQUEST.bytes));
    remote.fail('late');
    call.cancel();
    expect(ended).toHaveLength(1);
    expect(remote.cancelled).toBe(1);
    expect(received).toHaveLength(outcome === 'consumer' ? 0 : 1);
  }
  expect(decodeGatewayReply(gatewayEnd())).toMatchObject({case: 'end'});
});

test('synchronous port completion and failure still release ownership', async () => {
  let cancelled = 0;
  const ports: GatewayPortBridge = {
    open: (_request, receive) => {
      receive(gatewayReply(REQUEST.bytes));
      receive(gatewayEnd());
      return {
        cancel: () => {
          cancelled += 1;
        },
      };
    },
  };
  expect(await gatewayBridge(ports).unary('local', REQUEST.method, REQUEST.bytes).result).toEqual(
    REQUEST.bytes,
  );
  expect(cancelled).toBe(1);
  await expect(
    gatewayBridge({
      open: () => {
        throw new Error('cannot open');
      },
    }).unary('local', REQUEST.method, REQUEST.bytes).result,
  ).rejects.toThrow('cannot open');
});

test('a refusal keeps its gRPC status through the protobuf end, for a unary and a stream', async () => {
  for (const status of [0, 5, 7, 14, 16]) {
    const ports = new Ports();
    const call = gatewayBridge(ports).unary('local', REQUEST.method, REQUEST.bytes);
    const rejected = call.result.catch((error: unknown) => error);
    ports.last().reply(gatewayEnd('refused', status));
    const error = await rejected;
    expect(error).toBeInstanceOf(GatewayError);
    expect((error as GatewayError).status).toBe(status);
  }
  const ports = new Ports();
  const ended: Array<[string | undefined, number | undefined]> = [];
  gatewayBridge(ports).stream(
    'local',
    REQUEST.method,
    REQUEST.bytes,
    () => undefined,
    (error, status) => ended.push([error, status]),
  );
  ports.last().reply(gatewayEnd('gone', 5));
  expect(ended).toEqual([['gone', 5]]);
  expect(() => decodeGatewayReply(gatewayEnd('x', 17))).toThrow();
});

test('the generic bridge refuses Subscribe at both ends, for any host and either kind', async () => {
  const subscribe = SUBSCRIBE_METHOD;
  fc.assert(
    fc.property(
      fc.constantFrom('local', 'ssh:host-a', 'ssh:user@host-b'),
      fc.constantFrom('unary', 'stream'),
      fc.uint8Array({maxLength: 8}),
      (host, kind, bytes) => {
        const refused = {kind, host, method: subscribe, bytes};
        // The window refuses before it asks for a port.
        expect(() => encodeGatewayRequest(refused)).toThrow('subscription port');
        // Main refuses what a hostile renderer sent without going through the window's bridge.
        const sent = toBinary(
          DesktopGatewayRequestSchema,
          create(DesktopGatewayRequestSchema, {
            kind: kind === 'unary' ? DesktopGatewayKind.UNARY : DesktopGatewayKind.STREAM,
            host,
            method: subscribe,
            bytes,
          }),
        );
        expect(() => parseGatewayRequest(sent)).toThrow('subscription port');
      },
    ),
  );
  const ports = new Ports();
  const bridge = gatewayBridge(ports);
  // Like any request it can never send, it fails before a port is asked for.
  expect(() =>
    bridge.stream(
      'local',
      subscribe,
      new Uint8Array(),
      () => undefined,
      () => undefined,
    ),
  ).toThrow('subscription port');
  await expect(bridge.unary('local', subscribe, new Uint8Array()).result).rejects.toThrow(
    'subscription port',
  );
  expect(ports.calls).toHaveLength(0);
});
