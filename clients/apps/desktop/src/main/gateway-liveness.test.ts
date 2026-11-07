import type {Duplex} from 'node:stream';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, it} from '@effect/vitest';
import {API_SCHEMA_HASH, HelloResponseSchema} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import {LIVENESS_WINDOW_MS, SILENCE_BOUND_MS} from '../state/liveness.js';
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

/**
 * A connection whose sessions are controlled by the test: the peer speaks at each of `arrivals`
 * (virtual ms from the start, ascending), answers the handshake that opens a session, and never
 * answers a probe, as a link does that has stopped carrying bytes. Plays the arrivals, then
 * answers when each session was opened and when the last bytes arrived.
 */
const play = (arrivals: readonly number[]) =>
  Effect.gen(function* () {
    const openedAt: number[] = [];
    let heard = 0;
    const dialer: GatewayDialer = {dial: () => Effect.succeed(duplex)};
    const opener: SessionOpener = () =>
      Effect.gen(function* () {
        openedAt.push(yield* Effect.clockWith(clock => clock.currentTimeMillis));
        return {
          // The handshake that opens a session is answered; a probe never is.
          unary: () => Effect.succeed(hello),
          probe: () => Effect.never,
          call: () => Effect.never,
          lost: Effect.never,
          activity: Effect.sync(() => heard),
        } satisfies GatewaySession;
      });
    yield* makeGatewayConnection(dialer, quiet, opener);
    let now = 0;
    for (const arrival of arrivals) {
      yield* TestClock.adjust(arrival - now);
      heard += 1;
      now = arrival;
    }
    return {openedAt, last: now};
  });

const instant = Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 120_000}));
/**
 * The distinct instants of a draw, ascending: when the peer spoke. None falls on the end of a
 * window, where whether the bytes or the window's check come first is the clock's tie to break.
 */
const ascending = (draw: readonly number[]): number[] =>
  [...new Set(draw.map(at => (at % LIVENESS_WINDOW_MS === 0 ? at - 1 : at)))].sort((a, b) => a - b);

it.effect.prop(
  'redials a session that went silent, no later than the silence bound after its last bytes',
  {draw: Schema.Array(instant).check(Schema.isMaxLength(30))},
  ({draw}) =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = yield* play(ascending(draw));
        yield* TestClock.adjust(SILENCE_BOUND_MS);
        // Gaps of a window or more between the bytes may have cost earlier sessions; whatever
        // came before, exactly one redial follows the last bytes, and within the bound.
        const after = world.openedAt.filter(at => at > world.last);
        expect(after).toHaveLength(1);
        expect((after[0] ?? 0) - world.last).toBeLessThanOrEqual(SILENCE_BOUND_MS);
      }),
    ),
  {arbitrary: {runs: 30}},
);

it.effect.prop(
  'never redials a link that delivers within every window, however thinly',
  {
    gaps: Schema.Array(
      Schema.Int.check(Schema.isBetween({minimum: 1, maximum: LIVENESS_WINDOW_MS - 1})),
    )
      .check(Schema.isMinLength(1))
      .check(Schema.isMaxLength(40)),
  },
  ({gaps}) =>
    Effect.scoped(
      Effect.gen(function* () {
        let at = 0;
        const arrivals = gaps.map(gap => {
          at += gap;
          return at;
        });
        const world = yield* play(arrivals);
        expect(world.openedAt).toHaveLength(1);
      }),
    ),
  {arbitrary: {runs: 30}},
);
