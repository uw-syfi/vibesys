import {expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Stream from 'effect/Stream';
import {GatewayTransport} from '../../core/transport/gateway-transport.js';
import {assertNoCallbackErrors} from '../callback-errors.js';
import {makeFakeTransport} from './fake-transport.js';

it.effect('reports a throwing respond outside the transport failure boundary at teardown', () =>
  Effect.gen(function* () {
    const thrown = new Error('respond assertion failed');
    const fake = makeFakeTransport({
      respond: () => {
        throw thrown;
      },
    });
    yield* Effect.gen(function* () {
      const transport = yield* GatewayTransport;
      yield* transport
        .stream({host: 'local', method: '/test', request: new Uint8Array()}, 1)
        .pipe(Stream.runDrain);
    }).pipe(Effect.provide(fake.layer), Effect.exit);
    // The product may catch the defect. Teardown must still fail with the original error.
    expect(() => assertNoCallbackErrors()).toThrow('Fake callback failed');
    expect(() => assertNoCallbackErrors()).not.toThrow();
  }),
);
