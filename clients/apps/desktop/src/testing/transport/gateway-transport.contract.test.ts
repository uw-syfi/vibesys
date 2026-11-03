import * as Layer from 'effect/Layer';
import {GatewayPorts, GatewayTransportLive} from '../../core/transport/live.js';
import {gatewayTransportContract} from './contract.js';
import {makeFakeBridge} from './fake-bridge.js';
import {makeFakeTransport} from './fake-transport.js';

gatewayTransportContract('the fake transport', recording => {
  const fake = makeFakeTransport(recording);
  return {
    layer: fake.layer,
    calls: () => fake.calls().map(({open, released}) => ({open, released})),
  };
});

gatewayTransportContract('the gateway transport over a fake bridge', recording => {
  const fake = makeFakeBridge(recording);
  return {
    layer: GatewayTransportLive.pipe(
      Layer.provide(Layer.succeed(GatewayPorts, {bridge: fake.bridge})),
    ),
    // A port is open until it is released.
    calls: () => fake.ports().map(({cancels}) => ({open: cancels === 0, released: cancels})),
  };
});
