import {expect, test} from 'bun:test';
import {createRequire} from 'node:module';
import {encodeGatewayRequest, gatewayBridge, gatewayEnd, gatewayReply} from '@vibesys/ipc';

const {createGatewayPreload} = createRequire(import.meta.url)(
  './welcome.cjs',
) as typeof import('./welcome.cjs');

class Channel {
  received: (value: unknown) => void = () => {};
  failed: () => void = () => {};
  request: Uint8Array | undefined;
  closes = 0;
  sendFailure: Error | undefined;
  listen(receive: (value: unknown) => void, failed: () => void) {
    this.received = receive;
    this.failed = failed;
  }
  send(request: Uint8Array) {
    if (this.sendFailure !== undefined) throw this.sendFailure;
    this.request = request.slice();
  }
  close() {
    this.closes += 1;
  }
}
const REQUEST = {
  kind: 'unary' as const,
  host: 'local',
  method: '/vibesys.api.v1.HostService/Hello',
  bytes: new Uint8Array([1]),
};

test('gateway preload carries only protobuf bytes and releases a unary channel after its end', async () => {
  const channel = new Channel();
  const bridge = gatewayBridge(createGatewayPreload({channel: () => channel}));
  const call = bridge.unary(REQUEST.host, REQUEST.method, REQUEST.bytes);
  expect(channel.request).toEqual(encodeGatewayRequest(REQUEST));
  channel.received(gatewayReply(new Uint8Array([2])));
  channel.received(gatewayEnd());
  expect(await call.result).toEqual(new Uint8Array([2]));
  channel.received(gatewayReply(new Uint8Array([3])));
  channel.failed();
  call.cancel();
  expect(channel.closes).toBe(1);
});

test('cancellation, failed delivery, malformed IPC and send failure release their channel once', () => {
  for (const outcome of ['cancel', 'delivery', 'object', 'send'] as const) {
    const channel = new Channel();
    const received: Uint8Array[] = [];
    const errors: string[] = [];
    const bridge = createGatewayPreload({channel: () => channel});
    if (outcome === 'send') {
      channel.sendFailure = new Error('send failed');
      expect(() =>
        bridge.open(
          encodeGatewayRequest(REQUEST),
          value => received.push(value),
          error => errors.push(error),
        ),
      ).toThrow('send failed');
    } else {
      const call = bridge.open(
        encodeGatewayRequest(REQUEST),
        value => received.push(value),
        error => errors.push(error),
      );
      if (outcome === 'cancel') call.cancel();
      else if (outcome === 'delivery') channel.failed();
      else channel.received({bytes: REQUEST.bytes});
      channel.received(gatewayReply(REQUEST.bytes));
      channel.failed();
      call.cancel();
    }
    expect(channel.closes).toBe(1);
    expect(received).toEqual([]);
    expect(errors).toHaveLength(outcome === 'object' || outcome === 'delivery' ? 1 : 0);
  }
});
