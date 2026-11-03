import {create, toBinary} from '@bufbuild/protobuf';
import {expect, it} from '@effect/vitest';
import {
  Compatibility,
  HelloResponseSchema,
  OrchestratorService,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Cause from 'effect/Cause';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Schema from 'effect/Schema';
import * as Stream from 'effect/Stream';
import * as TestClock from 'effect/testing/TestClock';
import {settle} from '../../testing/settle.js';
import {
  type CallRecord,
  type FakeTransport,
  makeFakeTransport,
  type Recording,
  type Step,
} from '../../testing/transport/index.js';
import {type AppError, RpcStatus} from '../errors.js';
import {makeClient} from './make-client.js';

const HELLO = '/vibesys.orchestrator.v1.OrchestratorService/Hello';

const helloBytes = (compatibility: Compatibility): Uint8Array =>
  toBinary(
    HelloResponseSchema,
    create(HelloResponseSchema, {daemon: {binaryVersion: 'dev'}, compatibility}),
  );
const snapshotBytes = (sequence: bigint): Uint8Array =>
  toBinary(
    WatchRunsResponseSchema,
    create(WatchRunsResponseSchema, {body: {case: 'snapshot', value: {sequence}}}),
  );

const steps = (...all: Step[]): Recording => ({respond: () => all});

/** A client over a fake that plays `recording`, with the fake itself for inspection. */
const setup = (recording: Recording) => {
  const fake = makeFakeTransport(recording);
  const client = makeClient(OrchestratorService, 'local').pipe(Effect.provide(fake.layer));
  return {fake, client};
};
const HELLO_REQUEST = {
  client: {binaryVersion: 'desktop', protocolVersion: 1, minProtocolVersion: 1},
};

/** What each call has done so far, as `[open, cancelled]`. */
const states = (
  fake: FakeTransport,
): ReadonlyArray<readonly [CallRecord['open'], CallRecord['cancelled']]> =>
  fake.calls().map(call => [call.open, call.cancelled]);

/** The typed failure of an effect, or undefined when it succeeded. */
const failureOf = <A>(effect: Effect.Effect<A, AppError>) =>
  Effect.exit(effect).pipe(
    Effect.map(exit => (Exit.isFailure(exit) ? Cause.findErrorOption(exit.cause) : undefined)),
  );

it.effect('a unary call sends the descriptor path and host and returns the typed reply', () =>
  Effect.gen(function* () {
    const {fake, client} = setup(steps({frame: helloBytes(Compatibility.COMPATIBLE)}));
    const reply = yield* (yield* client).hello(HELLO_REQUEST);
    expect(reply.compatibility).toBe(Compatibility.COMPATIBLE);
    const [call] = fake.calls();
    expect(call?.target.method).toBe(HELLO);
    expect(call?.target.host).toBe('local');
    expect(call?.cancelled).toBe(0);
  }),
);

it.effect('a request that breaks its rules is refused before anything is sent', () =>
  Effect.gen(function* () {
    const {fake, client} = setup(steps({hang: true}));
    const api = yield* client;
    const unary = yield* Effect.flip(api.hello({}));
    const stream = yield* Effect.flip(
      Stream.runCollect(api.watchAgent({agentId: '', pageSize: 0})),
    );
    for (const error of [unary, stream]) {
      expect(error._tag).toBe('InvalidMessage');
      if (error._tag === 'InvalidMessage') expect(error.direction).toBe('request');
    }
    expect(unary._tag === 'InvalidMessage' && unary.detail).toContain('client');
    expect(stream._tag === 'InvalidMessage' && stream.detail).toContain('agent_id');
    expect(fake.calls()).toEqual([]);
  }),
);

it.effect('a reply that breaks its rules fails naming the field', () =>
  Effect.gen(function* () {
    const {client} = setup(steps({frame: helloBytes(Compatibility.UNSPECIFIED)}));
    const error = yield* Effect.flip((yield* client).hello(HELLO_REQUEST));
    expect(error._tag).toBe('InvalidMessage');
    if (error._tag !== 'InvalidMessage') return;
    expect(error.direction).toBe('response');
    expect(error.type).toBe('vibesys.orchestrator.v1.HelloResponse');
    expect(error.detail).toContain('compatibility');
  }),
);

it.effect('a stream emits whole valid frames and stops at the first bad one', () =>
  Effect.gen(function* () {
    const bad = Uint8Array.of(0xff, 0xff, 0xff);
    const {fake, client} = setup(
      steps({frame: snapshotBytes(1n)}, {frame: bad}, {frame: snapshotBytes(3n)}),
    );
    const seen: bigint[] = [];
    const exit = yield* Effect.exit(
      Stream.runForEach((yield* client).watchRuns({}), frame => {
        if (frame.body.case === 'snapshot') seen.push(frame.body.value.sequence);
        return Effect.void;
      }),
    );
    expect(seen).toEqual([1n]);
    expect(Exit.isFailure(exit)).toBe(true);
    expect(fake.calls().map(call => call.open)).toEqual([false]);
  }),
);

it.effect('a stream the daemon closes cleanly ends cleanly', () =>
  Effect.gen(function* () {
    const {client} = setup(steps({frame: snapshotBytes(1n)}, {close: true}));
    const frames = yield* Stream.runCollect((yield* client).watchRuns({}));
    expect(frames.length).toBe(1);
  }),
);

it.effect('a unary call that never answers fails at its deadline and is cancelled once', () =>
  Effect.gen(function* () {
    const {fake, client} = setup(steps({hang: true}));
    const fiber = yield* Effect.forkChild(
      Effect.flip((yield* client).hello(HELLO_REQUEST, {deadline: '5 seconds'})),
    );
    yield* TestClock.adjust('4999 millis');
    expect(fake.calls()[0]?.open).toBe(true);
    yield* TestClock.adjust('1 millis');
    const error = yield* Fiber.join(fiber);
    expect(error._tag).toBe('DeadlineExceeded');
    expect(error._tag === 'DeadlineExceeded' && error.origin).toBe('client');
    expect(states(fake)).toEqual([[false, 1]]);
  }),
);

it.effect('a stream with no first frame fails at its deadline; later silence is not bounded', () =>
  Effect.gen(function* () {
    const slow = makeClient(OrchestratorService, 'local').pipe(
      Effect.provide(makeFakeTransport(steps({hang: true})).layer),
    );
    const fiber = yield* Effect.forkChild(
      Effect.flip(Stream.runDrain((yield* slow).watchRuns({}, {deadline: '2 seconds'}))),
    );
    yield* TestClock.adjust('2 seconds');
    expect((yield* Fiber.join(fiber))._tag).toBe('DeadlineExceeded');

    const quiet = makeFakeTransport(steps({frame: snapshotBytes(1n)}, {hang: true}));
    const got: number[] = [];
    const consumer = yield* Effect.forkChild(
      Stream.runForEach(
        (yield* makeClient(OrchestratorService, 'local').pipe(
          Effect.provide(quiet.layer),
        )).watchRuns({}, {deadline: '2 seconds'}),
        () => Effect.sync(() => got.push(1)),
      ),
    );
    yield* settle;
    yield* TestClock.adjust('1 hour');
    expect(got).toEqual([1]);
    yield* Fiber.interrupt(consumer);
    expect(states(quiet)).toEqual([[false, 1]]);
  }),
);

// P3: interrupt cancels exactly once

/** Where, in virtual milliseconds, a call is interrupted, and what the recording does before. */
const interruptAt = Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 30}));
const gap = Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 10}));
const frameCount = Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 6}));
const ending = Schema.Literals(['hang', 'close', 'end']);

const script = (gapMs: number, frames: number, how: 'hang' | 'close' | 'end'): Step[] => [
  ...Array.from({length: frames}, (_, i): Step[] => [
    {sleep: `${gapMs} millis`},
    {frame: snapshotBytes(BigInt(i + 1))},
  ]).flat(),
  {sleep: `${gapMs} millis`},
  how === 'hang'
    ? {hang: true}
    : how === 'close'
      ? {close: true}
      : {end: {message: 'lost', status: RpcStatus.UNAVAILABLE}},
];

it.effect.prop(
  'interrupting a stream cancels it at most once, and no frame arrives afterwards',
  {at: interruptAt, gapMs: gap, frames: frameCount, how: ending},
  ({at, gapMs, frames, how}) =>
    Effect.gen(function* () {
      const {fake, client} = setup(steps(...script(gapMs, frames, how)));
      const seen: number[] = [];
      const fiber = yield* Effect.forkChild(
        Stream.runForEach((yield* client).watchRuns({}), () =>
          Effect.sync(() => seen.push(seen.length)),
        ),
      );
      yield* settle;
      yield* TestClock.adjust(`${at} millis`);
      yield* settle;
      yield* Fiber.interrupt(fiber);
      const delivered = seen.length;
      yield* TestClock.adjust('1 minute');
      const [call] = fake.calls();
      // Every frame whose time had come was delivered, and none came after the interrupt.
      const due = gapMs === 0 ? frames : Math.min(frames, Math.floor(at / gapMs));
      expect(delivered, JSON.stringify({at, gapMs, frames, how})).toBe(due);
      expect(seen.length).toBe(delivered);
      expect(call?.open).toBe(false);
      expect(call?.cancelled).toBeLessThanOrEqual(1);
      expect(fake.calls().length).toBe(1);
    }),
);

it.effect.prop(
  'interrupting a unary call cancels it at most once, and only if it was unanswered',
  {at: interruptAt, gapMs: gap, how: ending},
  ({at, gapMs, how}) =>
    Effect.gen(function* () {
      const reply: Step =
        how === 'hang'
          ? {hang: true}
          : how === 'close'
            ? {frame: helloBytes(Compatibility.COMPATIBLE)}
            : {end: {message: 'lost', status: RpcStatus.UNAVAILABLE}};
      const {fake, client} = setup(steps({sleep: `${gapMs} millis`}, reply));
      const fiber = yield* Effect.forkChild(Effect.exit((yield* client).hello(HELLO_REQUEST)));
      yield* TestClock.adjust(`${at} millis`);
      yield* Fiber.interrupt(fiber);
      const [call] = fake.calls();
      expect(call?.open).toBe(false);
      expect(call?.cancelled).toBeLessThanOrEqual(1);
      if (how !== 'hang' && at >= gapMs) expect(call?.cancelled).toBe(0);
    }),
);

// P4: error totality

const statusCode = Schema.UndefinedOr(
  Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 16})),
);

it.effect.prop(
  'a call that ends with any status fails with exactly one application error',
  {code: statusCode, message: Schema.String},
  ({code, message}) =>
    Effect.gen(function* () {
      const end = {message, ...(code === undefined ? {} : {status: code})};
      const {client} = setup(steps({end}));
      const unary = yield* Effect.flip((yield* client).hello(HELLO_REQUEST));
      const stream = yield* Effect.flip(Stream.runDrain((yield* client).watchRuns({})));
      expect(stream._tag).toBe(unary._tag);
    }),
);

it.effect.prop(
  'corrupted reply bytes give a valid message or InvalidMessage, never a defect',
  {bytes: Schema.Uint8Array},
  ({bytes}) =>
    Effect.gen(function* () {
      const {client} = setup(steps({frame: bytes}, {frame: bytes}));
      const api = yield* client;
      const unary = yield* failureOf(api.hello(HELLO_REQUEST));
      if (unary !== undefined && unary._tag === 'Some')
        expect(unary.value._tag).toBe('InvalidMessage');
      const frames: unknown[] = [];
      const stream = yield* failureOf(
        Stream.runForEach(api.watchRuns({}), frame => Effect.sync(() => frames.push(frame))),
      );
      // Either every frame was emitted whole, or the stream failed with InvalidMessage before any.
      if (stream !== undefined && stream._tag === 'Some') {
        expect(stream.value._tag).toBe('InvalidMessage');
        expect(frames).toEqual([]);
      }
    }),
);
