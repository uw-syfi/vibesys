import type {Duplex} from 'node:stream';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, it} from '@effect/vitest';
import {API_SCHEMA_HASH, HelloResponseSchema} from '@vibesys/proto';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Schema from 'effect/Schema';
import * as Scope from 'effect/Scope';
import * as TestClock from 'effect/testing/TestClock';
import {
  type GatewayDialer,
  makeGatewayConnection,
  type SessionOpener,
} from './gateway-connection.js';
import type {GatewaySession} from './grpc-session.js';

const hello = toBinary(
  HelloResponseSchema,
  create(HelloResponseSchema, {apiSchemaHash: API_SCHEMA_HASH, gatewayVersion: '0.1.0'}),
);
const quiet = () => {};
const duplex = {} as Duplex;

it.effect('closes an outstanding dial with its owning scope', () =>
  Effect.gen(function* () {
    const entered = yield* Deferred.make<void>();
    const cancelled = yield* Deferred.make<void>();
    const scope = yield* Scope.make();
    const dialer: GatewayDialer = {
      dial: () =>
        Deferred.succeed(entered, undefined).pipe(
          Effect.andThen(Effect.never),
          Effect.ensuring(Deferred.succeed(cancelled, undefined)),
        ),
    };
    yield* makeGatewayConnection(dialer, quiet).pipe(Effect.provideService(Scope.Scope, scope));
    yield* Deferred.await(entered);
    yield* Scope.close(scope, Exit.void);
    yield* Deferred.await(cancelled);
  }),
);

it.effect('attaches to a 0.1.0 host and redials after session loss and a long outage', () =>
  Effect.scoped(
    Effect.gen(function* () {
      const lost = yield* Deferred.make<void>();
      const firstOpened = yield* Deferred.make<void>();
      const retryEntered = yield* Deferred.make<void>();
      const recover = yield* Deferred.make<void>();
      let attempts = 0;
      const dialer: GatewayDialer = {
        dial: () =>
          Effect.gen(function* () {
            attempts += 1;
            if (attempts === 2) {
              yield* Deferred.succeed(retryEntered, undefined);
              yield* Deferred.await(recover);
            }
            return duplex;
          }),
      };
      const session: GatewaySession = {
        unary: () => Effect.succeed(hello),
        call: () => Effect.void,
        lost: Deferred.await(lost),
      };
      let opened = 0;
      const opener: SessionOpener = () =>
        Effect.suspend(() => {
          opened += 1;
          return Deferred.succeed(firstOpened, undefined).pipe(
            Effect.as(opened === 1 ? session : {...session, lost: Effect.never}),
          );
        });
      const connection = yield* makeGatewayConnection(dialer, quiet, opener);
      yield* Deferred.await(firstOpened);
      yield* Deferred.succeed(lost, undefined);
      yield* Deferred.await(retryEntered);
      yield* TestClock.adjust('30 seconds');
      yield* Deferred.succeed(recover, undefined);
      const call = yield* Effect.forkChild(connection.unary('/test/Call', new Uint8Array()));
      expect(yield* Fiber.join(call)).toEqual(hello);
      expect(attempts).toBeGreaterThanOrEqual(2);
    }),
  ),
);

it.effect.prop(
  'loss and cancellation sequences retire each session before opening the next',
  {losses: Schema.Array(Schema.Boolean).check(Schema.isMinLength(1), Schema.isMaxLength(5))},
  ({losses}) =>
    Effect.scoped(
      Effect.gen(function* () {
        const gates = yield* Effect.forEach(losses, () => Deferred.make<void>());
        const opened = yield* Effect.forEach(losses, () => Deferred.make<void>());
        const started = yield* Effect.forEach(losses, () => Deferred.make<void>());
        let count = 0;
        const dialer: GatewayDialer = {dial: () => Effect.succeed(duplex)};
        const opener: SessionOpener = () =>
          Effect.gen(function* () {
            const index = count++;
            const gate = gates[index];
            const signal = opened[index];
            const callStarted = started[index];
            if (signal !== undefined) yield* Deferred.succeed(signal, undefined);
            return {
              unary: () => Effect.succeed(hello),
              call: () =>
                callStarted === undefined
                  ? Effect.never
                  : Deferred.succeed(callStarted, undefined).pipe(Effect.andThen(Effect.never)),
              lost: gate === undefined ? Effect.never : Deferred.await(gate),
            };
          });
        const connection = yield* makeGatewayConnection(dialer, quiet, opener);
        for (const [index, shouldLose] of losses.entries()) {
          const signal = opened[index];
          const gate = gates[index];
          if (signal === undefined || gate === undefined) throw new Error('missing gate');
          yield* Deferred.await(signal);
          const call = yield* Effect.forkChild(
            connection.stream('/test/Call', new Uint8Array(), () => {}),
          );
          const callStarted = started[index];
          if (callStarted === undefined) throw new Error('missing call gate');
          yield* Deferred.await(callStarted);
          if (shouldLose) {
            yield* Deferred.succeed(gate, undefined);
            yield* Effect.ignore(Fiber.join(call));
          } else {
            yield* Fiber.interrupt(call);
            yield* Deferred.succeed(gate, undefined);
          }
        }
        expect(count).toBeGreaterThanOrEqual(losses.length);
      }),
    ),
);
