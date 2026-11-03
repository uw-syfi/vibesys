import type {Duplex} from 'node:stream';
import {describe, expect, it} from '@effect/vitest';
import {API_SCHEMA_HASH, DesktopLinkProblem} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as Layer from 'effect/Layer';
import * as Scope from 'effect/Scope';
import * as TestClock from 'effect/testing/TestClock';
import {makeHostConnection} from '../core/connection.js';
import {CallEnded, fromEnd, isRetryable} from '../core/errors.js';
import {decodeGatewayReply} from '../core/gateway-codec.js';
import {makeOrchestratorClient} from '../core/orchestrator-client.js';
import {GatewayPorts, GatewayTransportLive} from '../core/transport/live.js';
import {fakeGateway, HELLO} from '../testing/main/fake-gateway.js';
import {GatewayFailed, GrpcFailed, LinkFailed} from './errors.js';
import {type GatewayDialer, makeGatewayConnection} from './gateway-connection.js';
import {endOf} from './port-ipc.js';

const SERVE = '/vibesys.test.v1/Serve';
const OLD_HASH = '0'.repeat(64);
const bytes = (...values: number[]): Uint8Array => Uint8Array.from(values);

const until = (done: () => boolean) =>
  Effect.promise(async () => {
    while (!done()) await new Promise(resolve => setImmediate(resolve));
  });

type Step = 'ok' | GatewayFailed | 'never';

/** A dialer that follows `script` (then succeeds), recording each dial. */
function scriptedDialer(
  peer = fakeGateway(),
  script: Step[] = [],
  onDial: (force: boolean) => void = () => {},
) {
  const forces: boolean[] = [];
  const dialedAt: number[] = [];
  const dialer: GatewayDialer = {
    dial: force =>
      Effect.gen(function* () {
        forces.push(force);
        onDial(force);
        dialedAt.push(yield* Effect.clockWith(clock => clock.currentTimeMillis));
        const step = script.shift() ?? 'ok';
        if (step === 'never') return yield* Effect.never;
        if (step !== 'ok') return yield* Effect.fail(step);
        const duplex: Duplex = peer.connect();
        yield* Effect.addFinalizer(() => Effect.sync(() => duplex.destroy()));
        return duplex;
      }),
  };
  return {peer, dialer, forces, dialedAt};
}

const quiet = () => {};

describe('gateway connection', () => {
  it.effect('preserves a terminal SSH authentication failure through the port', () =>
    Effect.scoped(
      Effect.gen(function* () {
        let attempts = 0;
        const dialer: GatewayDialer = {
          dial: () =>
            Effect.suspend(() => {
              attempts += 1;
              return Effect.fail(new LinkFailed({problem: 'auth', message: 'key refused'}));
            }),
        };
        const connection = yield* makeGatewayConnection(dialer, quiet);
        const call = yield* Effect.forkChild(Effect.flip(connection.unary(SERVE, bytes())));
        yield* TestClock.adjust('10 seconds');
        const failure = yield* Fiber.join(call);
        expect(failure).toBeInstanceOf(LinkFailed);
        expect(attempts).toBe(1);
        const reply = decodeGatewayReply(endOf(failure));
        if (reply.case !== 'end') throw new Error('expected end');
        const appError = fromEnd(
          new CallEnded({
            message: reply.value.error ?? '',
            ...(reply.value.status === undefined ? {} : {status: reply.value.status}),
            ...(reply.value.linkProblem === undefined
              ? {}
              : {linkProblem: reply.value.linkProblem}),
          }),
        );
        expect(appError._tag).toBe('AuthenticationFailed');
        expect(isRetryable(appError)).toBe(false);
        let opens = 0;
        const layer = GatewayTransportLive.pipe(
          Layer.provide(
            Layer.succeed(GatewayPorts, {
              bridge: {
                open: (_request, receive) => {
                  opens += 1;
                  receive(endOf(failure));
                  return {cancel: () => {}};
                },
              },
            }),
          ),
        );
        const client = yield* makeOrchestratorClient('local').pipe(Effect.provide(layer));
        const visible = yield* makeHostConnection(client);
        const terminal = yield* Effect.flip(visible.ready());
        expect(terminal.state).toMatchObject({
          _tag: 'Unusable',
          fault: {kind: 'AuthenticationFailed', retryable: false},
        });
        yield* TestClock.adjust('10 minutes');
        expect(opens).toBe(1);
      }),
    ),
  );

  it.effect('carries capacity and unreachable link failures after the bounded retries', () =>
    Effect.scoped(
      Effect.gen(function* () {
        for (const [problem, expected] of [
          ['capacity', DesktopLinkProblem.CAPACITY],
          ['link', DesktopLinkProblem.LINK],
        ] as const) {
          let attempts = 0;
          const dialer: GatewayDialer = {
            dial: () =>
              Effect.suspend(() => {
                attempts += 1;
                return Effect.fail(new LinkFailed({problem, message: problem}));
              }),
          };
          const connection = yield* makeGatewayConnection(dialer, quiet);
          const call = yield* Effect.forkChild(Effect.flip(connection.unary(SERVE, bytes())));
          yield* TestClock.adjust('10 seconds');
          const failure = yield* Fiber.join(call);
          expect(attempts).toBe(5);
          const reply = decodeGatewayReply(endOf(failure));
          expect(reply.case === 'end' && reply.value.linkProblem).toBe(expected);
        }
      }),
    ),
  );

  it.effect('dials, shakes hands, and answers a call', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const {peer, dialer, forces} = scriptedDialer();
        const connection = yield* makeGatewayConnection(dialer, quiet);
        const call = yield* Effect.forkChild(connection.stream(SERVE, bytes(1), () => {}));
        yield* until(() => peer.calls.length === 1);
        peer.calls[0]?.end();
        yield* Fiber.join(call);
        expect(peer.methods).toEqual([HELLO, SERVE]);
        expect(forces).toEqual([false]);
        expect(yield* connection.ended).toBe(false);
      }),
    ),
  );

  it.effect('replaces the gateway once when its schema differs, and then connects', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        peer.schemaHash = OLD_HASH;
        const {dialer, forces} = scriptedDialer(peer, [], force => {
          // The forced dial installs the artifact this client was built with.
          if (force) peer.schemaHash = API_SCHEMA_HASH;
        });
        const connection = yield* makeGatewayConnection(dialer, quiet);
        const call = yield* Effect.forkChild(connection.stream(SERVE, bytes(1), () => {}));
        yield* until(() => peer.calls.length === 1);
        peer.calls[0]?.end();
        yield* Fiber.join(call);
        expect(forces).toEqual([false, true]);
      }),
    ),
  );

  it.effect('goes offline at once when a replaced gateway still disagrees', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        peer.schemaHash = OLD_HASH;
        const {dialer, forces} = scriptedDialer(peer);
        const connection = yield* makeGatewayConnection(dialer, quiet);
        const failure = yield* Effect.flip(connection.unary(SERVE, bytes()));
        expect(failure).toBeInstanceOf(GrpcFailed);
        expect(failure.message).toContain('API hash mismatch');
        const reply = decodeGatewayReply(endOf(failure));
        expect(reply.case === 'end' && reply.value.status).toBe(9);
        expect(forces).toEqual([false, true]);
        expect(yield* connection.ended).toBe(true);
      }),
    ),
  );

  it.effect('dials five times with growing waits, then is offline with the last reason', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const failure = new GatewayFailed({message: 'host unreachable'});
        const {dialer, dialedAt} = scriptedDialer(fakeGateway(), [
          failure,
          failure,
          failure,
          failure,
          failure,
        ]);
        const connection = yield* makeGatewayConnection(dialer, quiet);
        const call = yield* Effect.forkChild(Effect.flip(connection.unary(SERVE, bytes())));
        yield* TestClock.adjust('8 seconds');
        expect((yield* Fiber.join(call)).message).toBe('host unreachable');
        expect(dialedAt).toEqual([0, 500, 1500, 3500, 7500]);
        expect(yield* connection.ended).toBe(true);
      }),
    ),
  );

  it.effect('gives a dial that never answers 3 minutes, then counts it as a failure', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const {dialer, dialedAt} = scriptedDialer(fakeGateway(), ['never', 'never']);
        const connection = yield* makeGatewayConnection(dialer, quiet);
        const call = yield* Effect.forkChild(connection.unary(SERVE, bytes()).pipe(Effect.ignore));
        yield* TestClock.adjust('180 seconds');
        yield* TestClock.adjust('500 millis');
        expect(dialedAt).toEqual([0, 180_500]);
        yield* Fiber.interrupt(call);
      }),
    ),
  );

  it.effect(
    'fails a call in flight when the session is lost, and serves the next after redialing',
    () =>
      Effect.scoped(
        Effect.gen(function* () {
          const {peer, dialer, forces} = scriptedDialer();
          const connection = yield* makeGatewayConnection(dialer, quiet);
          const first = yield* Effect.forkChild(
            Effect.flip(connection.stream(SERVE, bytes(1), () => {})),
          );
          yield* until(() => peer.calls.length === 1);
          peer.drop();
          expect((yield* Fiber.join(first)).message).toBeTypeOf('string');
          const second = yield* Effect.forkChild(connection.stream(SERVE, bytes(2), () => {}));
          yield* until(() => peer.calls.length === 2);
          peer.calls[1]?.end();
          yield* Fiber.join(second);
          expect(forces).toEqual([false, false]);
          expect(peer.calls.map(call => Array.from(call.request.subarray(0)))).toEqual([[1], [2]]);
        }),
      ),
  );

  it.effect('closes the session with its scope and refuses later calls', () =>
    Effect.gen(function* () {
      const {peer, dialer} = scriptedDialer();
      const scope = yield* Scope.make();
      const connection = yield* makeGatewayConnection(dialer, quiet).pipe(
        Effect.provideService(Scope.Scope, scope),
      );
      const call = yield* Effect.forkChild(connection.stream(SERVE, bytes(1), () => {}));
      yield* until(() => peer.calls.length === 1);
      yield* Scope.close(scope, yield* Effect.exit(Effect.void));
      yield* Effect.ignore(Fiber.join(call));
      expect(yield* connection.ended).toBe(true);
      expect((yield* Effect.flip(connection.unary(SERVE, bytes()))).message).toContain('closed');
    }),
  );
});
