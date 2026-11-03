import {describe, expect, it} from '@effect/vitest';
import {Compatibility} from '@vibesys/orchestrator-proto';
import * as Clock from 'effect/Clock';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import type {HostConnectionState} from '../state/index.js';
import {
  type DaemonOptions,
  daemonRecording,
  FakeDaemon,
  generateScenario,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import {settle} from '../testing/settle.js';
import {makeFakeTransport} from '../testing/transport/index.js';
import {ConnectionTerminal, makeHostConnection} from './connection.js';
import {RpcStatus, Unavailable} from './errors.js';
import {makeOrchestratorClient} from './orchestrator-client.js';
import {ceilingMs, defaultReconnect, failuresAfter} from './reconnect.js';

const daemon = () => new FakeDaemon(generateScenario({seed: 1, subagents: 1, live: false}));
const HELLO = methodPathOf('Hello');

/** A connection over a fake transport that plays the daemon, read through a registry like a screen. */
const connect = (options: DaemonOptions = {}) =>
  Effect.gen(function* () {
    const fake = makeFakeTransport(daemonRecording(daemon(), options));
    const client = yield* makeOrchestratorClient('local').pipe(Effect.provide(fake.layer));
    const connection = yield* makeHostConnection(client);
    const registry = AtomRegistry.make();
    const state = Effect.gen(function* () {
      yield* settle;
      return registry.get(connection.state);
    });
    const hellos = () => fake.calls().filter(call => call.target.method === HELLO).length;
    return {fake, connection, state, hellos};
  });

type Connected = Effect.Success<ReturnType<typeof connect>>;

const loss = new Unavailable({detail: 'link lost'});

describe('the host connection', () => {
  it.effect('is Idle until something asks, and dials only then', () =>
    Effect.gen(function* () {
      const {state, hellos} = yield* connect();
      expect(yield* state).toEqual({_tag: 'Idle'});
      expect(hellos()).toBe(0);
    }),
  );

  it.effect('is Ready only after a COMPATIBLE Hello, at epoch 1', () =>
    Effect.gen(function* () {
      const {connection, state, hellos, fake} = yield* connect();
      const epoch = yield* connection.ready();
      expect(epoch).toBe(1);
      expect(yield* state).toMatchObject({_tag: 'Ready', epoch: 1});
      expect(hellos()).toBe(1);
      expect(fake.calls().map(call => call.target.method)).toEqual([HELLO]);
    }),
  );

  it.effect(
    'refuses an incompatible build, terminally and without retrying, until the user retries',
    () =>
      Effect.gen(function* () {
        const {connection, state, hellos} = yield* connect({
          compatibility: Compatibility.DAEMON_TOO_OLD,
        });
        const failure = yield* Effect.flip(connection.ready());
        expect(failure).toBeInstanceOf(ConnectionTerminal);
        expect(yield* state).toMatchObject({_tag: 'Incompatible', side: 'daemon-too-old'});
        yield* TestClock.adjust('10 minutes');
        expect(hellos()).toBe(1);
        yield* connection.retry;
        yield* settle;
        expect(hellos()).toBe(2);
      }),
  );

  it.effect('stops for good on a status a retry cannot change, and says why', () =>
    Effect.gen(function* () {
      const {connection, state, hellos} = yield* connect({
        helloFails: () => RpcStatus.UNIMPLEMENTED,
      });
      const failure = yield* Effect.flip(connection.ready());
      expect(failure.state).toMatchObject({_tag: 'Unusable', fault: {kind: 'Unimplemented'}});
      yield* TestClock.adjust('10 minutes');
      expect(yield* state).toMatchObject({_tag: 'Unusable'});
      expect(hellos()).toBe(1);
    }),
  );

  it.effect('waits and dials again after a lost Hello, and is Ready once one is answered', () =>
    Effect.gen(function* () {
      const {connection, state, hellos} = yield* connect({
        helloFails: nth => (nth < 2 ? RpcStatus.UNAVAILABLE : undefined),
      });
      const waiting = yield* Effect.forkChild(connection.ready());
      expect(yield* state).toMatchObject({_tag: 'Waiting', attempt: 1});
      yield* TestClock.adjust('500 millis');
      expect(yield* state).toMatchObject({_tag: 'Waiting', attempt: 2});
      yield* TestClock.adjust('1 second');
      expect(yield* Fiber.join(waiting)).toBe(1);
      expect(hellos()).toBe(3);
    }),
  );

  it.effect(
    'leaves Ready when a feed suspects the epoch, and a stale suspicion changes nothing',
    () =>
      Effect.gen(function* () {
        const {connection, state, hellos} = yield* connect();
        const first = yield* connection.ready();
        yield* connection.suspect(first + 1, loss);
        yield* settle;
        expect(yield* state).toMatchObject({_tag: 'Ready', epoch: 1});
        yield* connection.suspect(first, loss);
        expect(yield* state).toMatchObject({_tag: 'Waiting', attempt: 1});
        const next = yield* Effect.forkChild(connection.ready(first));
        yield* TestClock.adjust('500 millis');
        expect(yield* Fiber.join(next)).toBe(2);
        expect(hellos()).toBe(2);
      }),
  );

  it.effect('skips the wait when the user retries', () =>
    Effect.gen(function* () {
      const {connection, state, hellos} = yield* connect({
        helloFails: nth => (nth === 0 ? RpcStatus.UNAVAILABLE : undefined),
      });
      const waiting = yield* Effect.forkChild(connection.ready());
      expect(yield* state).toMatchObject({_tag: 'Waiting'});
      yield* connection.retry;
      expect(yield* Fiber.join(waiting)).toBe(1);
      expect(hellos()).toBe(2);
    }),
  );

  it.effect('an interrupted wait for Ready leaves the connection loop running', () =>
    Effect.gen(function* () {
      const {connection, state} = yield* connect({
        helloFails: nth => (nth === 0 ? RpcStatus.UNAVAILABLE : undefined),
      });
      const waiting = yield* Effect.forkChild(connection.ready());
      yield* settle;
      yield* Fiber.interrupt(waiting);
      yield* TestClock.adjust('500 millis');
      expect(yield* state).toMatchObject({_tag: 'Ready'});
    }),
  );
});

/** One step of a generated history: a Hello that is lost, or a Ready period of `up` ms then a loss. */
type Step = {readonly kind: 'fail'} | {readonly kind: 'up'; readonly up: number};
const between = (minimum: number, maximum: number) =>
  Schema.Int.check(Schema.isBetween({minimum, maximum}));
const steps = Schema.Array(
  Schema.Union([
    Schema.Struct({kind: Schema.Literal('fail')}),
    Schema.Struct({
      kind: Schema.Literal('up'),
      up: Schema.Union([between(0, 29_999), between(30_000, 120_000)]),
    }),
  ]),
).check(Schema.isMinLength(1), Schema.isMaxLength(14));

/** Play `step` against a connection that is waiting on its next Hello; answers the new count. */
const playStep = (connected: Connected, step: Step, count: number) =>
  Effect.gen(function* () {
    const {connection, state} = connected;
    if (step.kind === 'fail') return failuresAfter(defaultReconnect, count, null);
    const ready: HostConnectionState = yield* state;
    expect(ready._tag).toBe('Ready');
    yield* TestClock.adjust(step.up);
    yield* connection.suspect(ready._tag === 'Ready' ? ready.epoch : -1, loss);
    return failuresAfter(defaultReconnect, count, step.up);
  });

/** The wait the connection announced, checked against the ceiling for `count`, then lived through. */
const waitOut = (connected: Connected, count: number) =>
  Effect.gen(function* () {
    const waiting: HostConnectionState = yield* connected.state;
    if (waiting._tag !== 'Waiting') throw new Error(`expected Waiting, got ${waiting._tag}`);
    const wait = waiting.retryAtMs - (yield* Clock.currentTimeMillis);
    const ceiling = ceilingMs(defaultReconnect, count);
    expect(waiting.attempt).toBe(count);
    expect(wait).toBeGreaterThanOrEqual(Math.floor(ceiling / 2));
    expect(wait).toBeLessThanOrEqual(ceiling);
    yield* TestClock.adjust(wait);
  });

describe('the wait between attempts, over generated histories of losses', () => {
  it.effect.prop(
    'stays within the jittered ceiling for the count of failures since the last stable period',
    {history: steps},
    ({history}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const connected = yield* connect({
            helloFails: nth => (history[nth]?.kind === 'fail' ? RpcStatus.UNAVAILABLE : undefined),
          });
          const dial = yield* Effect.forkChild(connected.connection.ready());
          let count = 0;
          for (const step of history) {
            count = yield* playStep(connected, step, count);
            yield* waitOut(connected, count);
          }
          yield* Fiber.interrupt(dial);
        }),
      ),
    {arbitrary: {runs: 25}},
  );
});
