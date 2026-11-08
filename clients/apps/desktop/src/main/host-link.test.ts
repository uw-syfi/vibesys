/**
 * The whole path a window's host connection takes, from its feed through the connection policy,
 * the port, the main process's calls and sessions, to a real HTTP/2 peer, under the virtual clock.
 * Sequences of what a link does (a slow copy, a stall, a stream cut without a status, a dropped
 * socket, a hang, a refusal) are played against it, and the properties are the ones the app owes
 * a user whose host is remote: a copy that moves is never restarted, every loss is redialed
 * within the policy, every hang is noticed within the silence bound, a refusal is not retried.
 */
import type {Duplex} from 'node:stream';
import {gzip} from 'node:zlib';
import {create, toBinary} from '@bufbuild/protobuf';
import {expect, it} from '@effect/vitest';
import {
  BuildVersionsSchema,
  Compatibility,
  HelloResponseSchema,
  HostControlService,
  IdentifyResponseSchema,
  ListItemsRequestSchema,
  ListItemsResponseSchema,
  OrchestratorService,
  ReleaseRelation,
  RunsSnapshotSchema,
  StateRefusal,
  UnservedReason,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import {API_SCHEMA_HASH, DesktopRefusal} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import type * as Fiber from 'effect/Fiber';
import * as Layer from 'effect/Layer';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import {makeHostSession} from '../core/host-session.js';
import {Ids} from '../core/ids.js';
import {defaultReconnect} from '../core/reconnect.js';
import {GatewayPorts, GatewayTransportLive} from '../core/transport/live.js';
import {
  CONNECTION_LINGER_MS,
  COPY_STALL_MS,
  REPLY_STALL_MS,
  SLOW_ANSWER_BOUND_MS,
} from '../state/gateway-policy.js';
import {GATEWAY_TARGETS, type GatewayTarget} from '../state/gateway-target.js';
import {HOST_PREREQUISITES} from '../state/host-prerequisites.js';
import {type HostConnectionState, hostKey, hostStatusOf} from '../state/index.js';
import {LIVENESS_WINDOW_MS, SILENCE_BOUND_MS} from '../state/liveness.js';
import {type FakeCall, type FakeGateway, fakeGateway} from '../testing/main/fake-gateway.js';
import {fakeLocalHost} from '../testing/main/fake-local-host.js';
import {packInPlace} from '../testing/main/fake-pack.js';
import {fakeSshHost} from '../testing/main/fake-ssh-host.js';
import {portedBridge} from '../testing/main/ported-calls.js';
import {GatewayFailed, GatewayRefused} from './errors.js';
import {type GatewayCalls, makeGatewayCalls} from './gateway-calls.js';
import type {GatewayDialer} from './gateway-connection.js';
import {makeGatewayDialers} from './gateway-dialers.js';
import {GatewayArtifacts} from './gateway-provision.js';
import {makeLinkPool} from './link-pool.js';

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
    const calls = yield* makeGatewayCalls(
      () => dialer,
      () => {},
    );
    const seen = yield* windowOver(calls, peer, true);
    return {...seen, peer, dials};
  });

/**
 * A window on `calls`: the real host session over the port bridge, with its run feed mounted when
 * `feed`, and the helpers a test drives the virtual clock and reads the connection with.
 */
const windowOver = (calls: GatewayCalls, peer: FakeGateway, feed: boolean) =>
  Effect.gen(function* () {
    const services = yield* Effect.context<never>();
    const runWith = Effect.runPromiseExitWith(services);
    const bridge = portedBridge(calls, (effect, signal) => runWith(effect, {signal}));
    const transport = GatewayTransportLive.pipe(
      Layer.provide(Layer.succeed(GatewayPorts, {bridge})),
    );
    const session = yield* makeHostSession(HOST).pipe(
      Effect.provide(transport),
      Effect.provide(Ids.layer),
    );
    const registry = AtomRegistry.make();
    if (feed) registry.mount(session.runs.watch);
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
    const feedStatus = () => registry.get(session.runs.status);
    return {connection, live, feedStatus, elapse, until, epoch, hellos};
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

it.effect(
  'shows a host whose link stops delivering bytes as no longer live within the silence bound',
  () =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = yield* fresh({kind: 'copy', seconds: 1});
        expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
        const word = () =>
          hostStatusOf({
            connection: world.connection(),
            feed: world.feedStatus(),
            hasData: true,
            staleForMs: null,
          }).kind;
        expect(word()).toBe('live');
        yield* inflict(world, 'hang');
        // The word a screen shows leaves Live within the bound, and is Stale or Catching up, never
        // Live and never a word for a host that was not reached.
        const left = yield* world.until(
          () => word() !== 'live',
          SILENCE_BOUND_MS + NOTICE_SLACK_MS,
        );
        expect(left).not.toBeNull();
        expect(['stale', 'catching-up']).toContain(word());
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

// The class property: how a remote host fails, and what the app owes whoever is looking.
//
// Every world below is the whole real stack over in-memory processes: the link pool, the ssh link
// with its master, provisioning, the dialers, the calls and sessions, and a window on top. The host
// is a fake ssh host (`fake-ssh-host.ts`) whose master can freeze and whose build can be a dev
// build that holds fewer host programs than the host's platform needs.

const ALL_TARGETS = GATEWAY_TARGETS;
const PLATFORM = {
  'linux-amd64': 'Linux\nx86_64\n',
  'linux-arm64': 'Linux\naarch64\n',
  'darwin-arm64': 'Darwin\narm64\n',
  unsupported: 'Linux\nriscv64\n',
} as const;
type Platform = keyof typeof PLATFORM;

const HOST_PROGRAM_BYTES = new TextEncoder().encode('host-program');
const LIST_ITEMS = method('ListItems');
const IDENTIFY = `/${HostControlService.typeName}/${HostControlService.method.identify.name}`;
const listRequest = toBinary(
  ListItemsRequestSchema,
  create(ListItemsRequestSchema, {agentId: 'agent-1', beforeOrdinal: 1, pageSize: 10}),
);
/** A reply of a few hundred bytes, which a slow link delivers in as many chunks as a test says. */
const listReply = toBinary(
  ListItemsResponseSchema,
  create(ListItemsResponseSchema, {
    page: {
      elisions: Array.from({length: 300}, (_, index) => ({ordinal: index + 1, bodyBytes: 1n})),
    },
  }),
);

/** What the host says about why it does not serve the orchestrator. */
interface Unserving {
  readonly reason: UnservedReason;
  readonly stateRefusal: StateRefusal;
}

interface SshOptions {
  readonly platform: Platform;
  /** The host programs the app's own build holds: a dev build may hold fewer than all. */
  readonly built: readonly GatewayTarget[];
  readonly unserving?: Unserving;
  /** Prerequisites the host lacks, as `HOST_PREREQUISITES` names them. */
  readonly lacking?: readonly string[];
  /** The host already holds the program. */
  readonly provisioned?: boolean;
  /** The host's link carries bytes only as the test lets them through (`FakeGateway.throttle`). */
  readonly throttled?: boolean;
  /** The first orchestrator Hellos the link loses: cut without a status, or dropped. */
  readonly lostHellos?: {readonly how: 'cut' | 'drop'; readonly count: number};
  /**
   * The window's run feed: not mounted, mounted and answered at once, or mounted with its first
   * frame held back for the test to deliver.
   */
  readonly feed: 'off' | 'answered' | 'held';
}

/** A remote host over ssh as the real main process reaches it. */
const sshHost = (options: SshOptions) =>
  Effect.gen(function* () {
    const peer = fakeGateway();
    const host = fakeSshHost(peer, PLATFORM[options.platform]);
    for (const name of options.lacking ?? []) host.state.lacking.add(name);
    host.state.provisioned = options.provisioned ?? false;
    const pending: FakeCall[] = [];
    const logs: string[] = [];
    let lost = options.lostHellos?.count ?? 0;
    const {unserving} = options;
    const identity = toBinary(
      IdentifyResponseSchema,
      create(IdentifyResponseSchema, {
        release: 'dev',
        relation: ReleaseRelation.UNKNOWN,
        startedAt: {seconds: 1n, nanos: 0},
        orchestrator:
          unserving === undefined
            ? {case: 'serving', value: {}}
            : {
                case: 'unserved',
                value: {
                  reason: unserving.reason,
                  stateRefusal: unserving.stateRefusal,
                  detail: 'the state directory is refused',
                },
              },
      }),
    );
    const answerHello = (call: FakeCall): void => {
      if (lost > 0) {
        lost -= 1;
        if (options.lostHellos?.how === 'cut') call.endWithoutStatus();
        else peer.drop();
        return;
      }
      call.send(helloReply);
      call.end();
    };
    peer.onCall = call => {
      if (call.method === IDENTIFY) {
        call.send(identity);
        return call.end();
      }
      if (unserving !== undefined && call.method.startsWith(`/${OrchestratorService.typeName}/`))
        return call.end(14, 'the orchestrator is not served by this process');
      if (call.method === HELLO) return answerHello(call);
      // A feed's own stream is answered at once; anything else a test asks for waits for it.
      if (call.method === WATCH_RUNS && options.feed === 'answered')
        return call.send(snapshotReply);
      pending.push(call);
    };
    const built = Layer.succeed(GatewayArtifacts, {
      pack: packInPlace,
      read: target =>
        options.built.includes(target)
          ? Effect.succeed(HOST_PROGRAM_BYTES)
          : Effect.fail(
              new GatewayRefused({
                refusal: DesktopRefusal.HOST_PROGRAM_MISSING,
                message: `this build has no host program for ${target} (API ${API_SCHEMA_HASH.slice(0, 16)})`,
              }),
            ),
    });
    const dialers = yield* Effect.gen(function* () {
      const pool = yield* makeLinkPool('/tmp/vsd-1/%C', () => {});
      return yield* makeGatewayDialers(pool, () => {});
    }).pipe(Effect.provide(Layer.mergeAll(host.spawner.layer, fakeLocalHost().layer, built)));
    const calls = yield* makeGatewayCalls(dialers, (_host, line) => logs.push(line));
    const seen = yield* windowOver(calls, peer, options.feed !== 'off');
    const throttle = options.throttled === true ? peer.throttle() : undefined;
    const identifies = () => peer.methods.filter(name => name === IDENTIFY).length;
    return {...seen, calls, peer, host, pending, logs, identifies, throttle};
  });
type SshWorld = Effect.Success<ReturnType<typeof sshHost>>;

/** The time a host may take to be redialed after its master froze: noticing, tearing down, redialing. */
const FROZEN_BOUND_MS = SILENCE_BOUND_MS + 15_000 + defaultReconnect.capMs + 10_000;
const LINGER_SECONDS = CONNECTION_LINGER_MS / 1000;

const pieces = Schema.Int.check(Schema.isBetween({minimum: 2, maximum: 8}));
/** Gaps shorter than the stall window: every window of a progressing reply hears something. */
const gap = Schema.Int.check(Schema.isBetween({minimum: 1, maximum: REPLY_STALL_MS / 1000 - 1}));

const failure = Schema.Union([
  // A build that holds the host's program connects at once; one that does not refuses, naming the
  // platform and the version, and is never retried.
  Schema.Struct({
    kind: Schema.Literal('build'),
    platform: Schema.Literals([...ALL_TARGETS, 'unsupported']),
    built: Schema.Array(Schema.Literals(ALL_TARGETS)).check(Schema.isMaxLength(3)),
  }),
  // A host that lacks something the install needs refuses, naming each, and is never retried.
  Schema.Struct({
    kind: Schema.Literal('prerequisite'),
    lacking: Schema.Array(Schema.Literals(HOST_PREREQUISITES.map(({name}) => name)))
      .check(Schema.isMinLength(1))
      .check(Schema.isMaxLength(4)),
  }),
  // A link that loses the first Hellos is redialed: no host answered, so none is asked why, and
  // each loss is logged once.
  Schema.Struct({
    kind: Schema.Literal('lost'),
    how: Schema.Literals(['cut', 'drop']),
    count: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 3})),
  }),
  // A host that says why it does not serve: what it refuses for good is shown and not retried,
  // what may pass (held by another process) is waited out.
  Schema.Struct({
    kind: Schema.Literal('unserved'),
    reason: Schema.Literals([
      UnservedReason.STATE_REFUSED,
      UnservedReason.CONFIG_REFUSED,
      UnservedReason.HELD_ELSEWHERE,
      UnservedReason.HOLDER_GONE,
    ]),
    stateRefusal: Schema.Literals([
      StateRefusal.UNSPECIFIED,
      StateRefusal.NETWORK_FILESYSTEM,
      StateRefusal.UNKNOWN_FILESYSTEM,
    ]),
  }),
  // A reply, or a stream's first frame, that takes many windows to arrive but keeps arriving is
  // never cut; one that stops is cut within two windows of its last bytes.
  Schema.Struct({
    kind: Schema.Literal('slow'),
    // A call's reply, a stream's first frame, or the window's own feed's first frame.
    mode: Schema.Literals(['unary', 'stream', 'feed']),
    gap,
    pieces,
    stopAfter: Schema.Union([
      Schema.Undefined,
      Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 7})),
    ]),
  }),
  // Calls that follow each other within the linger share one dial; a master is not left behind.
  Schema.Struct({
    kind: Schema.Literal('linger'),
    gaps: Schema.Array(
      Schema.Int.check(Schema.isBetween({minimum: 0, maximum: LINGER_SECONDS - 2})),
    )
      .check(Schema.isMinLength(1))
      .check(Schema.isMaxLength(5)),
  }),
  // A master that freezes is killed by its pid and redialed within a bound, every time.
  Schema.Struct({
    kind: Schema.Literal('frozen'),
    quiet: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 30})),
    times: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 2})),
  }),
]);

/** One unary call to the host, whose answer the test delivers. */
const listItems = (world: SshWorld) =>
  Effect.gen(function* () {
    const running = yield* Effect.forkChild(
      Effect.result(world.calls.unary(HOST, LIST_ITEMS, listRequest)),
    );
    yield* world.until(() => world.pending.length > 0, 60_000);
    return {running, call: world.pending.shift()};
  });

const served: SshOptions = {platform: 'linux-amd64', built: ALL_TARGETS, feed: 'answered'};

const playBuild = (platform: Platform, built: readonly GatewayTarget[]) =>
  Effect.gen(function* () {
    const world = yield* sshHost({platform, built, feed: 'answered'});
    const target = platform === 'unsupported' ? undefined : platform;
    if (target !== undefined && built.includes(target)) {
      expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
      // One connect is one dial: the handshake and the feed share the connection.
      expect(world.host.state.connects).toBe(1);
      expect(world.host.state.masters).toBe(1);
      expect(world.host.state.uploads).toBe(1);
      return;
    }
    yield* world.until(() => world.connection()._tag === 'Unusable', 60_000);
    const state = world.connection();
    expect(state._tag).toBe('Unusable');
    if (state._tag !== 'Unusable') return;
    expect(state.fault.kind).toBe('Refused');
    expect(state.fault.cause).toBe(
      target === undefined ? 'unsupported-platform' : 'host-program-missing',
    );
    // It names the platform, and for a build that lacks the program, the version.
    expect(state.fault.detail).toContain(target ?? 'riscv64');
    if (target !== undefined) expect(state.fault.detail).toContain(API_SCHEMA_HASH.slice(0, 16));
    const probes = world.host.state.probes;
    yield* world.elapse(10 * 60_000, 30_000);
    // Never retried: nothing probed the host or dialed it again.
    expect(world.host.state.probes).toBe(probes);
    expect(world.host.state.connects).toBe(0);
    expect(world.connection()._tag).toBe('Unusable');
  });

const playPrerequisite = (lacking: readonly string[]) =>
  Effect.gen(function* () {
    const world = yield* sshHost({...served, lacking});
    yield* world.until(() => world.connection()._tag === 'Unusable', 60_000);
    const state = world.connection();
    expect(state._tag).toBe('Unusable');
    if (state._tag !== 'Unusable') return;
    expect(state.fault.kind).toBe('Refused');
    expect(state.fault.cause).toBe('host-prerequisite-missing');
    expect(state.fault.retryable).toBe(false);
    // It names everything the host lacks, and nothing was copied.
    for (const name of lacking) expect(state.fault.detail).toContain(name);
    expect(world.host.state.uploads).toBe(0);
    const probes = world.host.state.probes;
    yield* world.elapse(10 * 60_000, 30_000);
    // Never retried: the host was not probed or dialed again.
    expect(world.host.state.probes).toBe(probes);
    expect(world.host.state.connects).toBe(0);
    expect(world.connection()._tag).toBe('Unusable');
  });

/** The most drips a trickling link may need to carry a connect, however slow. */
const MOST_DRIPS = 800;
/** Drips after the connection is live: its liveness probes, answered at the same pace. */
const DRIPS_AFTER_LIVE = 12;

const playTrickle = (bytes: number, gap: number, stopAfter: number | undefined) =>
  Effect.gen(function* () {
    const world = yield* sshHost({...served, throttled: true});
    const cutLogged = () =>
      world.logs.some(line => line.includes('dial 1 failed') || line.includes('went silent'));
    let drips = 0;
    let liveFor = 0;
    // Every drip is progress: whatever the connect has taken so far, nothing is cut. A link that
    // never stops carries the connect to its end and a while beyond.
    while (stopAfter === undefined ? liveFor < DRIPS_AFTER_LIVE : drips < stopAfter) {
      expect(drips).toBeLessThan(MOST_DRIPS);
      yield* world.elapse(gap * 1000, 1000);
      world.throttle?.drip(bytes);
      drips += 1;
      if (world.live()) liveFor += 1;
      expect(cutLogged()).toBe(false);
      expect(world.host.state.connects).toBeLessThanOrEqual(1);
    }
    if (stopAfter === undefined) return;
    // The link stopped: cut within the bound of what it was doing, and not left connected.
    yield* world.elapse(Math.max(2 * REPLY_STALL_MS, SILENCE_BOUND_MS) + 2000, 500);
    expect(cutLogged()).toBe(true);
  });

const playLost = (how: 'cut' | 'drop', count: number) =>
  Effect.gen(function* () {
    const world = yield* sshHost({...served, lostHellos: {how, count}});
    expect(yield* world.until(() => world.live(), 120_000)).not.toBeNull();
    const failedHellos = world.logs.filter(line =>
      line.startsWith(`gateway call failed: ${HELLO}`),
    );
    // Each lost Hello is named once, and the host is not asked why it did not answer (no host
    // answered). It is asked once its link is Ready, for what stands in the way of a run: once.
    expect(failedHellos).toHaveLength(count);
    expect(yield* world.until(() => world.identifies() > 0, 60_000)).not.toBeNull();
    expect(world.identifies()).toBe(1);
  });

const REFUSED_CAUSE = {
  [StateRefusal.NETWORK_FILESYSTEM]: 'state-network-filesystem',
  [StateRefusal.UNKNOWN_FILESYSTEM]: 'state-unknown-filesystem',
  [StateRefusal.UNSPECIFIED]: 'state-refused',
} as const;

const playUnserved = (reason: UnservedReason, stateRefusal: StateRefusal) =>
  Effect.gen(function* () {
    const world = yield* sshHost({...served, unserving: {reason, stateRefusal}});
    const forGood =
      reason === UnservedReason.STATE_REFUSED || reason === UnservedReason.CONFIG_REFUSED;
    if (!forGood) {
      // Held by another process: waited out, asked again, never shown as a refusal.
      yield* world.elapse(60_000, 1000);
      expect(world.connection()._tag).not.toBe('Unusable');
      expect(world.hellos()).toBeGreaterThan(1);
      // The host answered UNAVAILABLE, so it was asked why.
      expect(world.identifies()).toBeGreaterThan(0);
      return;
    }
    yield* world.until(() => world.connection()._tag === 'Unusable', 60_000);
    const state = world.connection();
    expect(state._tag).toBe('Unusable');
    if (state._tag !== 'Unusable') return;
    expect(state.fault.kind).toBe('Refused');
    expect(state.fault.cause).toBe(
      reason === UnservedReason.CONFIG_REFUSED ? 'config-refused' : REFUSED_CAUSE[stateRefusal],
    );
    expect(state.fault.retryable).toBe(false);
    const asked = world.hellos();
    yield* world.elapse(10 * 60_000, 30_000);
    expect(world.hellos()).toBe(asked);
  });

it.effect('a host that already holds the program connects though it lacks gzip', () =>
  Effect.scoped(
    Effect.gen(function* () {
      const world = yield* sshHost({...served, lacking: ['gzip'], provisioned: true});
      // Nothing is installed, so nothing the install needs is required.
      expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
      expect(world.host.state.uploads).toBe(0);
    }),
  ),
);

/** The window's own feed, whose first frame the host takes many windows to deliver. */
const playSlowFeed = (spacing: number, count: number) =>
  Effect.gen(function* () {
    const world = yield* sshHost({...served, feed: 'held'});
    yield* world.until(() => world.pending.some(call => call.method === WATCH_RUNS), 60_000);
    const epoch = world.epoch();
    const sending = world.pending.shift()?.trickle(snapshotReply, count);
    for (let chunk = 1; chunk <= (sending?.parts ?? count); chunk += 1) {
      yield* world.elapse(spacing * 1000, 1000);
      sending?.next();
      // Whatever it has taken so far, the first frame is on its way: the connection is not redialed.
      expect(world.epoch()).toBe(epoch);
    }
    expect(yield* world.until(() => world.live(), 5000)).not.toBeNull();
    expect(world.epoch()).toBe(epoch);
  });

/** A call the test follows: what it runs as, and what it has heard. */
interface Followed {
  readonly world: SshWorld;
  readonly running: Fiber.Fiber<unknown>;
  readonly heard: Uint8Array[];
  readonly call: FakeCall | undefined;
}

/** Whether `running` ended in success with its own failure value, or is still going (undefined). */
const outcomeOf = (running: Fiber.Fiber<unknown>) => {
  const done = running.pollUnsafe();
  return done !== undefined && Exit.isSuccess(done) ? done.value : undefined;
};

/** Deliver `body` in chunks `spacing` seconds apart; a call that is cut short stops after `cut`. */
const deliver = (
  followed: Followed,
  body: Uint8Array,
  spacing: number,
  count: number,
  stopAfter: number | undefined,
) =>
  Effect.gen(function* () {
    const {world, running, call} = followed;
    const sending = call?.trickle(body, count);
    const parts = sending?.parts ?? count;
    // A message is cut short only if a part of it is still to come.
    const cut = stopAfter !== undefined && stopAfter < parts ? stopAfter : undefined;
    for (let chunk = 1; chunk <= parts; chunk += 1) {
      // The silence of a call that stops starts with its last chunk, so nothing waits before it.
      if (cut !== undefined && chunk > cut) break;
      yield* world.elapse(spacing * 1000, 1000);
      sending?.next();
      // Every chunk is progress: whatever the total so far, the call is not cut.
      expect(running.pollUnsafe()).toBeUndefined();
    }
    return cut !== undefined;
  });

/** The reply came whole: a call answers, a stream is not cut for being quiet after its first frame. */
const expectWhole = (followed: Followed, stream: boolean) =>
  Effect.gen(function* () {
    const {world, running, heard, call} = followed;
    yield* world.elapse(1000, 250);
    if (stream) {
      expect(heard).toHaveLength(1);
      yield* world.elapse(2 * REPLY_STALL_MS, 1000);
      expect(running.pollUnsafe()).toBeUndefined();
      return;
    }
    call?.end();
    yield* world.elapse(1000, 250);
    expect(outcomeOf(running)).toMatchObject({_tag: 'Success'});
  });

/**
 * The reply stopped while the host still answers the link's liveness probes: the link is alive, so
 * the call is not cut for silence, and is ended once `SLOW_ANSWER_BOUND_MS` has passed with nothing
 * of its own.
 */
const expectCut = (followed: Followed) =>
  Effect.gen(function* () {
    const {world, running} = followed;
    yield* world.elapse(2 * REPLY_STALL_MS, 1000);
    expect(running.pollUnsafe()).toBeUndefined();
    yield* world.elapse(SLOW_ANSWER_BOUND_MS, 5000);
    expect(outcomeOf(running)).toMatchObject({
      _tag: 'Failure',
      failure: {_tag: 'GrpcFailed', code: 4},
    });
  });

/** A reply that arrives in `count` chunks `spacing` seconds apart, stopping after `stopAfter` when given. */
const playSlow = (
  mode: 'unary' | 'stream' | 'feed',
  spacing: number,
  count: number,
  stopAfter: number | undefined,
) =>
  Effect.gen(function* () {
    if (mode === 'feed') return yield* playSlowFeed(spacing, count);
    const stream = mode === 'stream';
    const world = yield* sshHost({...served, feed: 'off'});
    const heard: Uint8Array[] = [];
    const running = yield* Effect.forkChild(
      Effect.result(
        stream
          ? world.calls.stream(HOST, WATCH_RUNS, new Uint8Array(0), bytes => heard.push(bytes))
          : world.calls.unary(HOST, LIST_ITEMS, listRequest),
      ),
    );
    yield* world.until(() => world.pending.length > 0, 60_000);
    const followed: Followed = {world, running, heard, call: world.pending.shift()};
    const cut = yield* deliver(
      followed,
      stream ? snapshotReply : listReply,
      spacing,
      count,
      stopAfter,
    );
    yield* cut ? expectCut(followed) : expectWhole(followed, stream);
  });

const playLinger = (gaps: readonly number[]) =>
  Effect.gen(function* () {
    const world = yield* sshHost({...served, feed: 'off'});
    const answer = Effect.gen(function* () {
      const {running, call} = yield* listItems(world);
      call?.send(listReply);
      call?.end();
      yield* world.elapse(500, 250);
      expect(running.pollUnsafe()).toBeDefined();
    });
    yield* answer;
    for (const seconds of gaps) {
      yield* world.elapse(seconds * 1000, 250);
      yield* answer;
    }
    // Calls within the linger shared the one dial, and the one master.
    expect(world.host.state.connects).toBe(1);
    expect(world.host.state.masters).toBe(1);
    // Past it, the connection is closed and the master with it; the next call dials anew.
    yield* world.elapse(CONNECTION_LINGER_MS + 2000, 500);
    expect(world.host.state.masterUp).toBe(false);
    yield* answer;
    expect(world.host.state.connects).toBe(2);
  });

const playFrozen = (quiet: number, times: number) =>
  Effect.gen(function* () {
    const world = yield* sshHost(served);
    expect(yield* world.until(() => world.live(), 60_000)).not.toBeNull();
    for (let round = 1; round <= times; round += 1) {
      yield* world.elapse(quiet * 1000, 1000);
      const before = world.epoch();
      const frozenPid = world.host.state.pid;
      const masters = world.host.state.masters;
      world.host.freeze();
      // Noticed within the silence bound; redialed on a fresh master within the frozen bound.
      expect(
        yield* world.until(() => world.epoch() !== before, SILENCE_BOUND_MS + NOTICE_SLACK_MS),
      ).not.toBeNull();
      expect(
        yield* world.until(() => world.live() && world.epoch() > before, FROZEN_BOUND_MS),
      ).not.toBeNull();
      expect(world.host.state.kills).toContain(frozenPid);
      expect(world.host.state.frozen).toBe(false);
      expect(world.host.state.masters).toBe(masters + 1);
    }
  });

it.effect.prop(
  'a remote host fails in any of its ways, and every failure is told apart: refusals name their cause and are not retried, progressing replies are not cut, one connect is one dial, a frozen master is redialed',
  {failure},
  ({failure}) =>
    Effect.scoped(
      Effect.gen(function* () {
        switch (failure.kind) {
          case 'build':
            return yield* playBuild(failure.platform, failure.built);
          case 'prerequisite':
            return yield* playPrerequisite(failure.lacking);
          case 'lost':
            return yield* playLost(failure.how, failure.count);
          case 'unserved':
            return yield* playUnserved(failure.reason, failure.stateRefusal);
          case 'slow':
            return yield* playSlow(failure.mode, failure.gap, failure.pieces, failure.stopAfter);
          case 'linger':
            return yield* playLinger(failure.gaps);
          case 'frozen':
            return yield* playFrozen(failure.quiet, failure.times);
        }
      }),
    ),
  {arbitrary: {runs: 20}},
);

// A link that delivers a few bytes at a time, however slowly (connection-level frames and the
// middle of frames included), is never cut while it delivers; once it stops, it is cut within the
// bound of whatever it was doing (a call's stall window, the liveness silence).
it.effect.prop(
  'a link that keeps delivering bytes is never cut, however slowly, and one that stops is cut within the bound',
  {
    bytes: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 3})),
    gap: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: LIVENESS_WINDOW_MS / 1000 - 1})),
    stopAfter: Schema.Union([
      Schema.Undefined,
      Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 400})),
    ]),
  },
  ({bytes, gap, stopAfter}) => Effect.scoped(playTrickle(bytes, gap, stopAfter)),
  {arbitrary: {runs: 25}},
);

/**
 * Replay of run 42450221478: a link that carried three bytes and stopped was not cut within the
 * bound, because the dial's own gzip waited on a busy thread pool while the virtual clock ran on.
 * The pool is kept busy here, so that the case fails at once if provisioning needs it again.
 */
it.effect('a link that stops is cut within the bound however busy the thread pool is', () =>
  Effect.scoped(
    Effect.gen(function* () {
      yield* Effect.sync(() => {
        for (let job = 0; job < 8; job += 1) gzip(Buffer.alloc(8_000_000, job), () => {});
      });
      yield* playTrickle(1, 3, 3);
    }),
  ),
);
