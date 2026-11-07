import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  ORCHESTRATOR_MAX_MESSAGE_BYTES,
  OrchestratorService,
  RunsSnapshotSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import {REPLY_STALL_MS} from '../state/gateway-policy.js';
import {fakeGateway, HELLO} from '../testing/main/fake-gateway.js';
import {GrpcFailed} from './errors.js';
import {openSession} from './grpc-session.js';

const bytes = (...values: number[]): Uint8Array => Uint8Array.from(values);
const SERVE = '/vibesys.test.v1/Serve';

/** Wait until the peer has received `count` scripted calls; the peer answers from real I/O. */
const received = (peer: ReturnType<typeof fakeGateway>, count: number) =>
  Effect.promise(async () => {
    while (peer.calls.length < count) await new Promise(resolve => setImmediate(resolve));
  });

describe('gateway session', () => {
  it.effect("answers a unary call with the peer's one message", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const reply = yield* session.unary(HELLO, new Uint8Array(0));
        expect(reply.byteLength).toBeGreaterThan(0);
        expect(peer.methods).toEqual([HELLO]);
      }),
    ),
  );

  it.effect.prop(
    "hands a stream's messages over in order, however the peer splits its frames",
    {
      messages: Schema.Array(Schema.Uint8Array.check(Schema.isMaxLength(40))).check(
        Schema.isMinLength(1),
        Schema.isMaxLength(6),
      ),
    },
    ({messages}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const peer = fakeGateway();
          const session = yield* openSession(peer.connect());
          const heard: Uint8Array[] = [];
          const running = yield* Effect.forkChild(
            session.call(SERVE, bytes(1), message => heard.push(Uint8Array.from(message))),
          );
          yield* received(peer, 1);
          const call = peer.calls[0];
          for (const message of messages) call?.send(message);
          call?.end();
          yield* Fiber.join(running);
          expect(heard).toEqual(messages);
        }),
      ),
    {arbitrary: {runs: 25}},
  );

  it.effect('ends a call with the status the peer ended it with', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const running = yield* Effect.forkChild(
          Effect.flip(session.call(SERVE, bytes(), () => {})),
        );
        yield* received(peer, 1);
        peer.calls[0]?.end(9, 'schema mismatch');
        const failure = yield* Fiber.join(running);
        expect(failure).toBeInstanceOf(GrpcFailed);
        expect(failure).toMatchObject({code: 9, message: 'schema mismatch'});
      }),
    ),
  );

  it.effect(
    'ends a call that carries no status as a lost link, which is retried, not refused',
    () =>
      Effect.scoped(
        Effect.gen(function* () {
          const peer = fakeGateway();
          const session = yield* openSession(peer.connect());
          const running = yield* Effect.forkChild(
            Effect.flip(session.call(SERVE, bytes(), () => {})),
          );
          yield* received(peer, 1);
          peer.calls[0]?.endWithoutStatus();
          const failure = yield* Fiber.join(running);
          expect(failure).toBeInstanceOf(GrpcFailed);
          // UNAVAILABLE (14), the status the client redials; UNKNOWN (2) would be shown as a refusal.
          expect(failure.code).toBe(14);
        }),
      ),
  );

  it.effect.prop(
    'ends a call with exactly the status the peer sent, whichever it is',
    {code: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 16}))},
    ({code}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const peer = fakeGateway();
          const session = yield* openSession(peer.connect());
          const running = yield* Effect.forkChild(
            Effect.flip(session.call(SERVE, bytes(), () => {})),
          );
          yield* received(peer, 1);
          peer.calls[0]?.end(code, 'said so');
          expect((yield* Fiber.join(running)).code).toBe(code);
        }),
      ),
    {arbitrary: {runs: 16}},
  );

  it.effect('counts what the peer sends, a chunk at a time, and nothing while it is silent', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const idle = yield* session.activity;
        yield* Effect.promise(() => new Promise(resolve => setImmediate(resolve)));
        expect(yield* session.activity).toBe(idle);
        const running = yield* Effect.forkChild(session.call(SERVE, bytes(), () => {}));
        yield* received(peer, 1);
        const asked = yield* session.activity;
        peer.calls[0]?.send(bytes(1));
        while ((yield* session.activity) === asked)
          yield* Effect.promise(() => new Promise(resolve => setImmediate(resolve)));
        peer.calls[0]?.end();
        yield* Fiber.join(running);
        expect(yield* session.activity).toBeGreaterThan(asked);
      }),
    ),
  );

  it.effect('ends a call whose consumer throws with INTERNAL, naming the consumer', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const running = yield* Effect.forkChild(
          Effect.flip(
            session.call(SERVE, bytes(), () => {
              throw new Error('refused');
            }),
          ),
        );
        yield* received(peer, 1);
        peer.calls[0]?.send(bytes(7));
        const failure = yield* Fiber.join(running);
        expect(failure.code).toBe(13);
        expect(failure.message).toContain('refused');
      }),
    ),
  );

  it.effect('resets the stream of a call that is interrupted, and only that one', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const first = yield* Effect.forkChild(session.call(SERVE, bytes(1), () => {}));
        const second = yield* Effect.forkChild(session.call(SERVE, bytes(2), () => {}));
        yield* received(peer, 2);
        yield* Fiber.interrupt(first);
        yield* Effect.promise(async () => {
          while (peer.calls[0]?.cancelled() !== true) await new Promise(r => setImmediate(r));
        });
        expect(peer.calls[1]?.cancelled()).toBe(false);
        peer.calls[1]?.end();
        yield* Fiber.join(second);
      }),
    ),
  );

  it.effect('reports a lost session, fails the calls under it, and refuses new ones', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const running = yield* Effect.forkChild(
          Effect.flip(session.call(SERVE, bytes(), () => {})),
        );
        yield* received(peer, 1);
        peer.drop();
        yield* session.lost;
        expect((yield* Fiber.join(running)).code).toBe(14);
        expect((yield* Effect.flip(session.unary(SERVE, bytes()))).code).toBe(14);
      }),
    ),
  );

  it.effect('refuses a method that is not a gRPC path, and a message above the limit', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        expect((yield* Effect.flip(session.unary('Serve', bytes()))).message).toContain('invalid');
        const huge = new Uint8Array(64 * 1024 * 1024 + 1);
        expect((yield* Effect.flip(session.unary(SERVE, huge))).code).toBe(8);
        expect(peer.methods).toEqual([]);
      }),
    ),
  );

  it.effect('fails a unary call that heard nothing for a window as a deadline, within two', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const peer = fakeGateway();
        const session = yield* openSession(peer.connect());
        const running = yield* Effect.forkChild(Effect.flip(session.unary(SERVE, bytes())));
        yield* received(peer, 1);
        // The bound is on silence, between one and two windows after the last bytes.
        yield* TestClock.adjust(`${2 * REPLY_STALL_MS} millis`);
        expect((yield* Fiber.join(running)).code).toBe(4);
      }),
    ),
  );

  it.effect('ends the session with its scope', () =>
    Effect.gen(function* () {
      const peer = fakeGateway();
      const session = yield* Effect.scoped(openSession(peer.connect()));
      yield* session.lost;
    }),
  );

  it.effect(
    'accepts a packed WatchRuns snapshot and refuses an over-limit orchestrator reply',
    () =>
      Effect.scoped(
        Effect.gen(function* () {
          const snapshotBudget = 1024 * 1024;
          const snapshot = create(RunsSnapshotSchema, {
            sequence: 1n,
            searches: Array.from({length: 4}, (_, i) => ({
              runId: `run-${i}`,
              revision: 1n,
              health: {state: {case: 'liveSince' as const, value: {seconds: 0n, nanos: 0}}},
              plans: [{number: 1, body: 'x'.repeat(snapshotBudget / 4 - 1024)}],
            })),
          });
          const packed = toBinary(
            WatchRunsResponseSchema,
            create(WatchRunsResponseSchema, {
              body: {case: 'snapshot', value: snapshot},
            }),
          );
          expect(packed.byteLength).toBeLessThanOrEqual(snapshotBudget + 5);
          expect(packed.byteLength).toBeGreaterThan(snapshotBudget - 8192);
          const peer = fakeGateway();
          const session = yield* openSession(peer.connect());
          const method = `/${OrchestratorService.typeName}/${OrchestratorService.method.watchRuns.name}`;
          const heard: Uint8Array[] = [];
          const watching = yield* Effect.forkChild(
            session.call(method, bytes(), message => heard.push(Uint8Array.from(message))),
          );
          yield* received(peer, 1);
          peer.calls[0]?.send(packed);
          peer.calls[0]?.end();
          yield* Fiber.join(watching);
          expect(heard).toEqual([packed]);
          const oversized = yield* Effect.forkChild(
            Effect.flip(session.call(method, bytes(), () => {})),
          );
          yield* received(peer, 2);
          peer.calls[1]?.send(new Uint8Array(ORCHESTRATOR_MAX_MESSAGE_BYTES + 1));
          peer.calls[1]?.end();
          expect((yield* Fiber.join(oversized)).code).toBe(8);
        }),
      ),
  );
});
