import {expect, it} from '@effect/vitest';
import {gatewayEnd, gatewayReply, parseGatewayRequest} from '@vibesys/ipc';
import * as Cause from 'effect/Cause';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Layer from 'effect/Layer';
import * as Option from 'effect/Option';
import * as Schema from 'effect/Schema';
import * as Stream from 'effect/Stream';
import {settle} from '../../testing/settle.js';
import {type FakeBridge, makeFakeBridge, type OpenedPort} from '../../testing/transport/index.js';
import {GatewayTransport} from './gateway-transport.js';
import {GatewayPorts, GatewayTransportLive} from './live.js';

const TARGET = {host: 'local', method: '/p.S/M', request: Uint8Array.of(7)};
const frame = (n: number): Uint8Array => gatewayReply(Uint8Array.of(n));

const world = (): {fake: FakeBridge; layer: Layer.Layer<GatewayTransport>} => {
  const fake = makeFakeBridge();
  const layer = GatewayTransportLive.pipe(
    Layer.provide(Layer.succeed(GatewayPorts, {bridge: fake.bridge})),
  );
  return {fake, layer};
};
const bytesOf = (frames: Iterable<Uint8Array>): number[] => [...frames].map(f => f[0] ?? -1);

it.effect('a unary call sends its routing and returns the reply, releasing the port once', () => {
  const {fake, layer} = world();
  return Effect.gen(function* () {
    const transport = yield* GatewayTransport;
    const fiber = yield* Effect.forkChild(transport.unary(TARGET));
    yield* settle;
    const [port] = fake.ports();
    expect(parseGatewayRequest(port?.request)).toEqual({
      kind: 'unary',
      host: 'local',
      method: '/p.S/M',
      bytes: Uint8Array.of(7),
    });
    port?.reply(frame(5));
    port?.reply(gatewayEnd());
    expect(yield* Fiber.join(fiber)).toEqual(Uint8Array.of(5));
    expect(fake.ports()[0]?.cancels).toBe(1);
  }).pipe(Effect.provide(layer));
});

it.effect('a unary call that ends with a failure carries the status, or none', () =>
  Effect.gen(function* () {
    const cases: [(port: OpenedPort) => void, unknown][] = [
      [port => port.reply(gatewayEnd('gone', 14)), {message: 'gone', status: 14}],
      [port => port.reply(gatewayEnd('cancelled')), {message: 'cancelled', status: undefined}],
      [port => port.fail('port lost'), {message: 'port lost', status: undefined}],
      [port => port.reply(Uint8Array.of(255, 255)), undefined],
      [port => port.reply(gatewayEnd()), {message: 'missing unary response', status: undefined}],
    ];
    for (const [drive, expected] of cases) {
      const {fake, layer} = world();
      const fiber = yield* Effect.forkChild(
        Effect.flip(
          Effect.gen(function* () {
            return yield* (yield* GatewayTransport).unary(TARGET);
          }).pipe(Effect.provide(layer)),
        ),
      );
      yield* settle;
      const port = fake.ports()[0];
      if (port !== undefined) drive(port);
      const error = yield* Fiber.join(fiber);
      expect(error._tag).toBe('CallEnded');
      if (expected !== undefined && error._tag === 'CallEnded')
        expect({message: error.message, status: error.status}).toEqual(expected);
      expect(fake.ports()[0]?.cancels).toBe(1);
    }
  }),
);

it.effect('without a gateway every call fails with NoBridge', () =>
  Effect.gen(function* () {
    const transport = yield* GatewayTransport;
    expect((yield* Effect.flip(transport.unary(TARGET)))._tag).toBe('NoBridge');
    expect((yield* Effect.flip(Stream.runDrain(transport.stream(TARGET, 4))))._tag).toBe(
      'NoBridge',
    );
  }).pipe(
    Effect.provide(
      GatewayTransportLive.pipe(Layer.provide(Layer.succeed(GatewayPorts, {bridge: undefined}))),
    ),
  ),
);

it.effect('interrupting a unary call cancels its port once', () => {
  const {fake, layer} = world();
  return Effect.gen(function* () {
    const fiber = yield* Effect.forkChild((yield* GatewayTransport).unary(TARGET));
    yield* settle;
    yield* Fiber.interrupt(fiber);
    expect(fake.ports().map(port => port.cancels)).toEqual([1]);
  }).pipe(Effect.provide(layer));
});

/** Run a stream call to its end against a fresh fake, with `drive` playing the main process. */
const runStream = (capacity: number, drive: (port: OpenedPort) => void) => {
  const {fake, layer} = world();
  return Effect.gen(function* () {
    const seen: Uint8Array[] = [];
    const fiber = yield* Effect.forkChild(
      Effect.exit(
        Stream.runForEach((yield* GatewayTransport).stream(TARGET, capacity), bytes =>
          Effect.sync(() => seen.push(bytes)),
        ),
      ),
    );
    yield* settle;
    const port = fake.ports()[0];
    if (port !== undefined) drive(port);
    yield* settle;
    const exit = yield* Fiber.join(fiber);
    return {seen: bytesOf(seen), exit, cancels: fake.ports().map(p => p.cancels)};
  }).pipe(Effect.provide(layer));
};

const failureTag = (exit: Exit.Exit<unknown, unknown>): string | undefined => {
  const error = Exit.isFailure(exit) ? Cause.findErrorOption(exit.cause) : undefined;
  return error !== undefined && Option.isSome(error)
    ? (error.value as {_tag: string})._tag
    : undefined;
};

it.effect('a stream delivers frames in order and ends cleanly', () =>
  Effect.gen(function* () {
    const run = yield* runStream(8, port => {
      for (const n of [1, 2, 3]) port.reply(frame(n));
      port.reply(gatewayEnd());
    });
    expect(run.seen).toEqual([1, 2, 3]);
    expect(Exit.isSuccess(run.exit)).toBe(true);
    expect(run.cancels).toEqual([1]);
  }),
);

it.effect('a stream that ends with a failure delivers what came first, then fails', () =>
  Effect.gen(function* () {
    const run = yield* runStream(8, port => {
      port.reply(frame(1));
      port.reply(gatewayEnd('lost', 14));
      port.reply(frame(2));
    });
    expect(run.seen).toEqual([1]);
    expect(failureTag(run.exit)).toBe('CallEnded');
    expect(run.cancels).toEqual([1]);
  }),
);

it.effect.prop(
  'a stream holds at most its capacity of unread frames: one more cuts it, cancelled once',
  {
    capacity: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 6})),
    extra: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 3})),
  },
  ({capacity, extra}) => {
    const {fake, layer} = world();
    return Effect.gen(function* () {
      const fiber = yield* Effect.forkChild(
        Effect.exit(Stream.runDrain((yield* GatewayTransport).stream(TARGET, capacity))),
      );
      yield* settle;
      const port = fake.ports()[0];
      // All frames arrive before the reader takes any: nothing yields between them.
      for (let n = 0; n < capacity + extra; n += 1) port?.reply(frame(n));
      port?.reply(gatewayEnd());
      yield* settle;
      const exit = yield* Fiber.join(fiber);
      expect(failureTag(exit)).toBe(extra > 0 ? 'StreamOverflow' : undefined);
      expect(fake.ports().map(p => p.cancels)).toEqual([1]);
    }).pipe(Effect.provide(layer));
  },
);

it.effect.prop(
  'interrupting a stream at any point cancels its port once and delivers nothing after',
  {
    before: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 4})),
    after: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 4})),
  },
  ({before, after}) => {
    const {fake, layer} = world();
    return Effect.gen(function* () {
      const seen: Uint8Array[] = [];
      const fiber = yield* Effect.forkChild(
        Stream.runForEach((yield* GatewayTransport).stream(TARGET, 16), bytes =>
          Effect.sync(() => seen.push(bytes)),
        ),
      );
      yield* settle;
      const port = fake.ports()[0];
      for (let n = 0; n < before; n += 1) port?.reply(frame(n));
      yield* settle;
      yield* Fiber.interrupt(fiber);
      const delivered = seen.length;
      for (let n = 0; n < after; n += 1) port?.reply(frame(n));
      port?.reply(gatewayEnd());
      yield* settle;
      expect(delivered).toBe(before);
      expect(seen.length).toBe(delivered);
      expect(fake.ports().map(p => p.cancels)).toEqual([1]);
    }).pipe(Effect.provide(layer));
  },
);
