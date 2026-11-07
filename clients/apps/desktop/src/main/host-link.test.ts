/**
 * The whole path a window's host connection takes, from its feed through the connection policy,
 * the port, the main process's calls and sessions, to a real HTTP/2 peer, under the virtual clock.
 * Sequences of what a link does (a slow copy, a stall, a stream cut without a status, a dropped
 * socket, a hang, a refusal) are played against it, and the properties are the ones the app owes
 * a user whose host is remote: a copy that moves is never restarted, every loss is redialed
 * within the policy, every hang is noticed within the silence bound, a refusal is not retried.
 */
import type {Duplex} from 'node:stream';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, it} from '@effect/vitest';
import {
  BuildVersionsSchema,
  Compatibility,
  HelloResponseSchema,
  OrchestratorService,
  RunsSnapshotSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import {makeHostSession} from '../core/host-session.js';
import {Ids} from '../core/ids.js';
import {defaultReconnect} from '../core/reconnect.js';
import {GatewayPorts, GatewayTransportLive} from '../core/transport/live.js';
import {COPY_STALL_MS} from '../state/gateway-policy.js';
import {type HostConnectionState, hostKey} from '../state/index.js';
import {SILENCE_BOUND_MS} from '../state/liveness.js';
import {fakeGateway} from '../testing/main/fake-gateway.js';
import {portedBridge} from '../testing/main/ported-calls.js';
import {GatewayFailed} from './errors.js';
import {makeGatewayCalls} from './gateway-calls.js';
import type {GatewayDialer} from './gateway-connection.js';

const HOST = hostKey({kind: 'ssh', alias: 'host-a'});
const method = (name: string) => `/${OrchestratorService.typeName}/${name}`;
const HELLO = method('Hello');
const WATCH_RUNS = method('WatchRuns');

const helloReply = toBinary(
  HelloResponseSchema,
  create(HelloResponseSchema, {
    daemon: create(BuildVersionsSchema, {
      binaryVersion: 'fake',
      protocolVersion: 1,
      minProtocolVersion: 1,
    }),
    compatibility: Compatibility.COMPATIBLE,
  }),
);
const snapshotReply = toBinary(
  WatchRunsResponseSchema,
  create(WatchRunsResponseSchema, {
    body: {case: 'snapshot', value: create(RunsSnapshotSchema, {sequence: 1n})},
  }),
);

/** Real event-loop turns the HTTP/2 peer and the streams need between two moves of the clock. */
const IO_TURNS = 30;
const STEP_MS = 250;
/** What a cut, a drop or a hang may add to the time it takes to be seen: the turns it crosses. */
const NOTICE_SLACK_MS = 1_000;
const turns = Effect.promise(async () => {
  for (let turn = 0; turn < IO_TURNS; turn += 1)
    await new Promise(resolve => setImmediate(resolve));
});

type Copy =
  /** The host program is copied for this many seconds, each second of it progress. */
  | {readonly kind: 'copy'; readonly seconds: number}
  /** The copy stops being accepted: the dial fails once the stall window passes. */
  | {readonly kind: 'stall'; readonly seconds: number};

/** What the peer does to the orchestrator calls it receives. */
interface Peer {
  /** Answer the next orchestrator Hello without a status, as a cut link does. */
  cutNextHello: boolean;
  /** Answer every orchestrator Hello with this status. */
  refuseHello: number | undefined;
}

/** A remote host seen from a window: the real call layer over a scripted dialer and peer. */
const remoteHost = (copy: Copy, behaviour: Peer) =>
  Effect.gen(function* () {
    const peer = fakeGateway();
    const dials = {copies: 0, interrupted: 0};
    let copied = false;
    peer.onCall = call => {
      if (call.method === HELLO) {
        if (behaviour.cutNextHello) {
          behaviour.cutNextHello = false;
          return call.endWithoutStatus();
        }
        if (behaviour.refuseHello !== undefined) return call.end(behaviour.refuseHello, 'refused');
        call.send(helloReply);
        return call.end();
      }
      if (call.method === WATCH_RUNS) call.send(snapshotReply);
    };
    const dialer: GatewayDialer = {
      dial: () =>
        Effect.gen(function* () {
          if (!copied) {
            // The first dial copies the host program; later dials find it on the host.
            dials.copies += 1;
            for (let second = 0; second < copy.seconds; second += 1)
              yield* Effect.sleep('1 second');
            // A stalled copy fails the dial, and the host keeps nothing of it.
            if (copy.kind === 'stall' && dials.copies === 1)
              return yield* new GatewayFailed({
                message: `gateway upload stalled for ${COPY_STALL_MS / 1000} seconds`,
              });
            copied = true;
          }
          const duplex: Duplex = peer.connect();
          yield* Effect.addFinalizer(() => Effect.sync(() => duplex.destroy()));
          return duplex;
        }).pipe(
          Effect.onInterrupt(() =>
            Effect.sync(() => {
              dials.interrupted += 1;
            }),
          ),
        ),
    };
    const services = yield* Effect.context<never>();
    const runWith = Effect.runPromiseExitWith(services);
    const calls = yield* makeGatewayCalls(
      () => dialer,
      () => {},
    );
    const bridge = portedBridge(calls, (effect, signal) => runWith(effect, {signal}));
    const transport = GatewayTransportLive.pipe(
      Layer.provide(Layer.succeed(GatewayPorts, {bridge})),
    );
    const session = yield* makeHostSession(HOST).pipe(
      Effect.provide(transport),
      Effect.provide(Ids.layer),
    );
    const registry = AtomRegistry.make();
    registry.mount(session.runs.watch);
    // Each world ends with its run: a feed left mounted would keep redialing a closed host on the
    // clock the next world shares.
    yield* Effect.addFinalizer(() => Effect.sync(() => registry.dispose()));
    const connection = (): HostConnectionState => registry.get(session.connection.state);
    const live = (): boolean => registry.get(session.runs.status)._tag === 'Live';
    const elapse = (ms: number, step = STEP_MS) =>
      Effect.gen(function* () {
        yield* turns;
        for (let passed = 0; passed < ms; passed += step) {
          yield* TestClock.adjust(step);
          yield* turns;
        }
      });
    /** Step until `done`, answering how long it took (ms), or null when `limit` ms was not enough. */
    const until = (done: () => boolean, limit: number) =>
      Effect.gen(function* () {
        for (let passed = 0; passed <= limit; passed += STEP_MS) {
          if (done()) return passed;
          yield* elapse(STEP_MS);
        }
        return done() ? limit : null;
      });
    const epoch = (): number => {
      const state = connection();
      return state._tag === 'Ready' ? state.epoch : -1;
    };
    const hellos = () => peer.calls.filter(call => call.method === HELLO).length;
    return {peer, dials, connection, live, elapse, until, epoch, hellos};
  });

const second = Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 120}));
const upTo = <S extends Schema.Top>(max: number, item: S) =>
  Schema.Array(item).check(Schema.isMinLength(1)).check(Schema.isMaxLength(max));

/** What a link does to a connected host. */
type Fault = 'cut' | 'drop' | 'hang';
const faults = Schema.Literals(['cut', 'drop', 'hang']);

/** Inflict `fault`; answers how long it may take the window to notice, at the most. */
const inflict = (world: Effect.Success<ReturnType<typeof remoteHost>>, fault: Fault) =>
  Effect.sync(() => {
    const {peer} = world;
    if (fault === 'hang') {
      peer.hang();
      return SILENCE_BOUND_MS;
    }
    if (fault === 'drop') peer.drop();
    else
      peer.calls
        .filter(call => call.method === WATCH_RUNS)
        .at(-1)
        ?.endWithoutStatus();
    return 0;
  });

const fresh = (copy: Copy, behaviour: Peer = {cutNextHello: false, refuseHello: undefined}) =>
  remoteHost(copy, behaviour);

it.effect('connects a fresh host whose program takes 40 seconds to copy, copying it once', () =>
  Effect.scoped(
    Effect.gen(function* () {
      const world = yield* fresh({kind: 'copy', seconds: 40});
      expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
      // Not abandoned at 15 seconds and started over: the copy ran to its end, once.
      expect(world.dials.interrupted).toBe(0);
      expect(world.dials.copies).toBe(1);
    }),
  ),
);

it.effect('redials a stream that ended without a status instead of showing a refusal', () =>
  Effect.scoped(
    Effect.gen(function* () {
      const world = yield* fresh({kind: 'copy', seconds: 1});
      expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
      const before = world.epoch();
      yield* inflict(world, 'cut');
      expect(
        yield* world.until(() => world.live() && world.epoch() > before, 40_000),
      ).not.toBeNull();
      expect(world.connection()._tag).toBe('Ready');
    }),
  ),
);

it.effect('notices a link that hangs within the silence bound, and recovers on its own', () =>
  Effect.scoped(
    Effect.gen(function* () {
      const world = yield* fresh({kind: 'copy', seconds: 1});
      expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
      const before = world.epoch();
      yield* inflict(world, 'hang');
      const noticed = yield* world.until(() => world.epoch() !== before, SILENCE_BOUND_MS + 1_000);
      // Frozen data shown as live for no longer than the bound; the old 53 to 92 seconds is gone.
      expect(noticed).not.toBeNull();
      expect(
        yield* world.until(() => world.live() && world.epoch() > before, 40_000),
      ).not.toBeNull();
    }),
  ),
);

it.effect.prop(
  'a host that is slow to first connect is dialed once, and every later loss is redialed in time',
  {
    copy: second,
    stalled: Schema.Boolean,
    helloCut: Schema.Boolean,
    history: upTo(
      4,
      Schema.Struct({
        quiet: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 60})),
        fault: faults,
      }),
    ),
  },
  ({copy, stalled, helloCut, history}) =>
    Effect.scoped(
      Effect.gen(function* () {
        const behaviour: Peer = {cutNextHello: helloCut, refuseHello: undefined};
        const world = yield* remoteHost(
          {kind: stalled ? 'stall' : 'copy', seconds: copy},
          behaviour,
        );
        // A copy that moves is never restarted, however long a first connection takes. A copy that
        // stalls fails its dial once, and the redial after the policy's wait copies again. A Hello
        // cut without a status is a lost link, redialed.
        const connected = yield* world.until(
          () => world.live(),
          (stalled ? 2 : 1) * copy * 1000 + defaultReconnect.capMs * 3,
        );
        expect(connected).not.toBeNull();
        expect(world.dials.interrupted).toBe(0);
        expect(world.dials.copies).toBe(stalled ? 2 : 1);
        for (const {quiet, fault} of history) {
          yield* world.elapse(quiet * 1000, 1000);
          const before = world.epoch();
          const noticeBound = yield* inflict(world, fault);
          // Noticed: the connection left the Ready period it was in, perhaps to be back already.
          const noticed = yield* world.until(
            () => world.epoch() !== before,
            noticeBound + NOTICE_SLACK_MS,
          );
          // Every hang is noticed within the silence bound; a cut or a drop at once.
          expect(noticed).not.toBeNull();
          // Every loss is redialed within the reconnect policy's longest wait, and the feed reads again.
          const back = yield* world.until(
            () => world.live() && world.epoch() > before,
            noticeBound + defaultReconnect.capMs + 10_000,
          );
          expect(back).not.toBeNull();
        }
      }),
    ),
  {arbitrary: {runs: 12}},
);

it.effect.prop(
  'a host that refuses a Hello with a status of its own is not redialed',
  {
    status: Schema.Literals([1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16]),
  },
  ({status}) =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = yield* remoteHost(
          {kind: 'copy', seconds: 1},
          {cutNextHello: false, refuseHello: status},
        );
        yield* world.until(() => world.connection()._tag === 'Unusable', 30_000);
        expect(world.connection()._tag).toBe('Unusable');
        const asked = world.hellos();
        // However long it waits, the connection does not ask again until the user retries.
        yield* world.elapse(10 * 60_000, 30_000);
        expect(world.hellos()).toBe(asked);
        expect(world.connection()._tag).toBe('Unusable');
      }),
    ),
  {arbitrary: {runs: 10}},
);
