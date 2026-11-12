import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  Compatibility,
  SearchStateSchema,
  SnapshotDetail,
  UnreadableReason,
  WatchAgentRequestSchema,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import * as Atom from 'effect/reactivity/Atom';
import * as Schema from 'effect/Schema';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  applyChange,
  applySnapshot,
  emptyHostModel,
  emptyState,
  type FeedStatus,
  type Freshness,
  type HostModel,
  type OrchestratorState,
} from '../state/index.js';
import {
  FakeDaemon,
  faultsOn,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {FaultPlan, Recording, Step} from '../testing/transport/index.js';
import {RpcStatus} from './errors.js';
import {runFeed} from './feeds.js';
import {MAX_QUIET_RESTARTS, STABLE_PASS_FRAMES} from './policy.js';
import {defaultReconnect, delayMs} from './reconnect.js';

const SEED = 20261103;
const between = (minimum: number, maximum: number) =>
  Schema.Int.check(Schema.isBetween({minimum, maximum}));
/** At most `max` values of `item`. */
const upTo = <S extends Schema.Top>(max: number, item: S) =>
  Schema.Array(item).check(Schema.isMaxLength(max));
const scenario = generateScenario({seed: 7, subagents: 2, live: false});
const TOTAL = scenario.changes.length;
const daemonAt = (visible: number = TOTAL) => new FakeDaemon(scenario, visible);

/**
 * What the run stream defines of the state: the runs and what the host says of each (a snapshot and the
 * frames after it agree on it). A run's agents and turns are its history, which a screen reads on request.
 */
const runStreamShape = (s: OrchestratorState) => ({
  sequence: s.sequence,
  runs: s.runs,
  summaries: s.summaries,
  gap: s.gap,
});
const everything = runStreamShape({
  ...scenario.changes.reduce(applyChange, emptyState),
  summaries: Object.fromEntries(
    new FakeDaemon(scenario).summaries().map(summary => [summary.runId, summary]),
  ),
});

const agentIdOf = (daemon: FakeDaemon): string => {
  const [agent] = daemon.snapshot().agents;
  if (agent === undefined) throw new Error('the scenario has no agent');
  return agent.id;
};

const callsTo = (h: HostHarness, method: string) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf(method));
const openStreams = (h: HostHarness) =>
  h.fake.calls().filter(call => call.kind === 'stream' && call.open);
const runsRequests = (h: HostHarness) =>
  callsTo(h, 'WatchRuns').map(call => fromBinary(WatchRunsRequestSchema, call.target.request));

describe('the run stream', () => {
  it.effect('goes Live after a snapshot and holds the host state', () =>
    Effect.gen(function* () {
      const daemon = daemonAt();
      const h = yield* hostHarness(daemon);
      h.mount(h.session.runs.watch);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      expect(runStreamShape((yield* h.model).data).sequence).toBe(daemon.sequence);
      const freshness: Freshness = yield* h.read(h.session.runs.freshness);
      expect(freshness).toBe('live');
    }),
  );

  it.effect('says it follows nothing once unmounted', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(daemonAt());
      const unmount = h.mount(h.session.runs.watch);
      yield* h.elapse(1);
      expect(yield* h.read(h.session.runs.freshness)).toBe('live');
      unmount();
      yield* h.elapse(1);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Opening'});
      expect(yield* h.read(h.session.runs.freshness)).not.toBe('live');
    }),
  );

  it.effect('dials only when mounted, and closes its stream when unmounted', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(daemonAt());
      yield* h.elapse(10);
      expect(h.fake.calls()).toEqual([]);
      const unmount = h.mount(h.session.runs.watch);
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(1);
      unmount();
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(0);
    }),
  );

  it.effect(
    'refreshes from a replacement SUMMARY snapshot and keeps the old rows until it arrives',
    () =>
      Effect.gen(function* () {
        const daemon = daemonAt();
        const h = yield* hostHarness(daemon, {
          wrap: base => ({
            respond: (target, nth) =>
              target.method === methodPathOf('WatchRuns') && nth === 1
                ? [{sleep: '10 seconds'}, ...base.respond(target, nth)]
                : base.respond(target, nth),
          }),
        });
        h.mount(h.session.runs.watch);
        expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
        const before = runStreamShape((yield* h.model).data);

        // A manual replacement is authoritative even when the host restarted below the sequence
        // already shown. Until it arrives, the old model remains whole on screen.
        daemon.loseTail(Math.max(1, Math.floor(TOTAL / 2)));
        const replacement = daemon.snapshot(SnapshotDetail.SUMMARY);
        const expected = runStreamShape(applySnapshot(emptyState, replacement));
        yield* h.session.runs.refresh;
        yield* h.session.runs.refresh;
        yield* h.session.runs.refresh;
        expect(yield* h.read(h.session.runs.refreshState)).toEqual({_tag: 'Refreshing'});
        expect(runStreamShape((yield* h.model).data)).toEqual(before);
        const refreshingCalls = callsTo(h, 'WatchRuns');
        expect(refreshingCalls).toHaveLength(2);
        expect(refreshingCalls.map(call => ({open: call.open, released: call.released}))).toEqual([
          {open: false, released: 1},
          {open: true, released: 0},
        ]);
        expect(runsRequests(h)[1]).toMatchObject({
          afterSequence: 0n,
          detail: SnapshotDetail.SUMMARY,
        });

        yield* h.elapse(9);
        expect(yield* h.read(h.session.runs.refreshState)).toEqual({_tag: 'Refreshing'});
        expect(runStreamShape((yield* h.model).data)).toEqual(before);
        yield* h.elapse(1);
        expect(yield* h.read(h.session.runs.refreshState)).toEqual({
          _tag: 'Refreshed',
          sequence: replacement.sequence,
        });
        expect(runStreamShape((yield* h.model).data)).toEqual(expected);

        // Refreshed is an acknowledgement, not a toast timer; it stays until another request.
        yield* h.elapse(3_600, 3_600);
        expect(yield* h.read(h.session.runs.refreshState)).toEqual({
          _tag: 'Refreshed',
          sequence: replacement.sequence,
        });
        expect(callsTo(h, 'WatchRuns')).toHaveLength(2);
      }),
  );

  it.effect('reports a refused replacement and leaves the existing RetryGate behavior intact', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(daemonAt(), {
        wrap: base => ({
          respond: (target, nth) =>
            target.method === methodPathOf('WatchRuns') && nth === 1
              ? [{end: {message: 'runs unavailable', status: RpcStatus.NOT_FOUND}}]
              : base.respond(target, nth),
        }),
      });
      h.mount(h.session.runs.watch);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      const before = runStreamShape((yield* h.model).data);

      yield* h.session.runs.refresh;
      expect(yield* h.read(h.session.runs.refreshState)).toEqual({_tag: 'Failed'});
      expect(yield* h.read(h.session.runs.status)).toMatchObject({
        _tag: 'Failed',
        fault: {kind: 'NotFound'},
      });
      expect(runStreamShape((yield* h.model).data)).toEqual(before);
      expect(runsRequests(h).map(request => request.afterSequence)).toEqual([0n, 0n]);

      // The old Retry action still releases a failed feed and resumes at its cursor.
      yield* h.session.runs.retry;
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      expect(runsRequests(h).map(request => request.afterSequence)).toEqual([
        0n,
        0n,
        BigInt(TOTAL),
      ]);
      expect(yield* h.read(h.session.runs.refreshState)).toEqual({_tag: 'Failed'});

      // A later Refresh is a new request and can replace the recovered feed.
      yield* h.session.runs.refresh;
      expect(yield* h.read(h.session.runs.refreshState)).toEqual({
        _tag: 'Refreshed',
        sequence: BigInt(TOTAL),
      });
      expect(runsRequests(h).map(request => request.afterSequence)).toEqual([
        0n,
        0n,
        BigInt(TOTAL),
        0n,
      ]);
    }),
  );

  it.effect('keeps a refresh request across an unmount and completes it on the next mount', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(daemonAt(), {
        wrap: base => ({
          respond: (target, nth) =>
            target.method === methodPathOf('WatchRuns') && nth === 1
              ? [{sleep: '10 seconds'}, ...base.respond(target, nth)]
              : base.respond(target, nth),
        }),
      });
      const unmount = h.mount(h.session.runs.watch);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      yield* h.session.runs.refresh;
      expect(yield* h.read(h.session.runs.refreshState)).toEqual({_tag: 'Refreshing'});
      unmount();
      yield* h.elapse(0);

      h.mount(h.session.runs.watch);
      expect(yield* h.read(h.session.runs.refreshState)).toEqual({
        _tag: 'Refreshed',
        sequence: BigInt(TOTAL),
      });
      expect(runsRequests(h).map(request => request.afterSequence)).toEqual([0n, 0n, 0n]);
      expect(callsTo(h, 'WatchRuns').map(call => call.released)).toEqual([1, 1, 0]);
    }),
  );

  // P1: a stream interrupted by any link faults ends with the state an uninterrupted one has.
  describe('under generated faults (P1)', () => {
    interface Schedule {
      readonly startTenths: number;
      readonly commits: ReadonlyArray<number>;
      readonly plan: FaultPlan;
      readonly helloFails: ReadonlyArray<number>;
    }
    const schedules = Schema.Struct({
      startTenths: between(1, 9),
      commits: upTo(3, between(1, 60)),
      plan: Schema.Struct({
        dropAfter: upTo(4, between(0, 6)),
        duplicate: upTo(3, between(0, 5)),
      }),
      helloFails: upTo(2, between(0, 3)),
    });

    const play = (schedule: Schedule) =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = daemonAt(Math.floor((TOTAL * schedule.startTenths) / 10));
          const failing = new Set(schedule.helloFails);
          const h = yield* hostHarness(daemon, {
            // Streams close after their frames, so each reopen sees what was committed meanwhile.
            closeStreams: true,
            helloFails: nth => (failing.has(nth) ? RpcStatus.UNAVAILABLE : undefined),
            wrap: faultsOn('WatchRuns', schedule.plan),
          });
          h.mount(h.session.runs.watch);
          for (const commit of schedule.commits) {
            yield* h.elapse(40, 5);
            daemon.commit(commit);
          }
          daemon.commit(TOTAL);
          yield* h.elapse(600, 5);
          return runStreamShape((yield* h.model).data);
        }),
      );

    it.effect.prop(
      'ends in the state of an uninterrupted stream',
      {schedule: schedules},
      ({schedule}) =>
        Effect.gen(function* () {
          expect(yield* play(schedule).pipe(Random.withSeed(SEED))).toEqual(everything);
        }),
      {arbitrary: {runs: 20}},
    );
  });

  describe('a gap in the stream', () => {
    const START = Math.floor(TOTAL / 2);
    const skippedEvent = scenario.changes[START + 1];

    /** The frame of an event two ahead of the snapshot, so one event is missing. */
    const gapFrame = (): Step | undefined =>
      skippedEvent === undefined
        ? undefined
        : {
            frame: toBinary(
              WatchRunsResponseSchema,
              create(WatchRunsResponseSchema, {body: {case: 'change', value: skippedEvent}}),
            ),
          };

    /** The snapshot of `steps` followed by the skipped event, when there is one to skip. */
    const withGap = (steps: ReadonlyArray<Step>): ReadonlyArray<Step> => {
      const [snapshot] = steps;
      const gap = gapFrame();
      return snapshot === undefined || gap === undefined ? steps : [snapshot, gap, {hang: true}];
    };

    /** A run stream that reads a snapshot and then skips an event: always, or on the first call only. */
    const gappedFor =
      (always: boolean) =>
      (base: Recording): Recording => ({
        respond: (target, nth) => {
          const steps = base.respond(target, nth);
          if (target.method !== methodPathOf('WatchRuns')) return steps;
          const {afterSequence} = fromBinary(WatchRunsRequestSchema, target.request);
          return afterSequence === 0n && (always || nth === 0) ? withGap(steps) : steps;
        },
      });

    const requestedAfter = (h: HostHarness) =>
      callsTo(h, 'WatchRuns').map(
        call => fromBinary(WatchRunsRequestSchema, call.target.request).afterSequence,
      );

    it.effect('reopens from a snapshot, not from the cursor, and recovers', () =>
      Effect.gen(function* () {
        const h = yield* hostHarness(daemonAt(START), {wrap: gappedFor(false)});
        h.mount(h.session.runs.watch);
        yield* h.elapse(1);
        expect(requestedAfter(h)).toEqual([0n, 0n]);
        expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
        expect((yield* h.model).data.gap).toBeNull();
        expect(callsTo(h, 'Hello')).toHaveLength(1);
      }),
    );

    it.effect('that never clears makes the feed blame the connection after four restarts', () =>
      Effect.gen(function* () {
        const h = yield* hostHarness(daemonAt(START), {wrap: gappedFor(true)});
        h.mount(h.session.runs.watch);
        yield* h.elapse(0);
        expect(callsTo(h, 'WatchRuns')).toHaveLength(4);
        expect(callsTo(h, 'Hello')).toHaveLength(1);
        yield* h.elapse(60);
        expect(callsTo(h, 'Hello').length).toBeGreaterThan(1);
      }),
    );
  });
});

describe('a Hello that is not COMPATIBLE (P7)', () => {
  const outcomes = Schema.Struct({
    compatibility: Schema.Literals([
      Compatibility.COMPATIBLE,
      Compatibility.CLIENT_TOO_OLD,
      Compatibility.DAEMON_TOO_OLD,
    ]),
    failures: between(0, 3),
    status: Schema.Literals([RpcStatus.UNAVAILABLE, RpcStatus.UNIMPLEMENTED]),
  });

  type Outcome = typeof outcomes.Type;

  const play = (outcome: Outcome) =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = daemonAt();
        const h = yield* hostHarness(daemon, {
          compatibility: outcome.compatibility,
          helloFails: nth => (nth < outcome.failures ? outcome.status : undefined),
        });
        h.mount(h.session.runs.watch);
        h.mount(h.session.agents.watch(agentIdOf(daemon)));
        yield* h.elapse(120, 5);
        return {h, freshness: yield* h.read(h.session.runs.freshness)};
      }).pipe(Random.withSeed(SEED)),
    );

  it.effect.prop(
    'opens no stream until one has been answered COMPATIBLE',
    {outcome: outcomes},
    ({outcome}) =>
      Effect.gen(function* () {
        const {h, freshness} = yield* play(outcome);
        // What the host says about why it does not serve is asked beside the handshake, not by it.
        const calls = h.fake.calls().filter(call => !call.target.method.includes('HostControl'));
        const firstStream = calls.findIndex(call => call.kind === 'stream');
        const helloAnswered = outcome.failures === 0 || outcome.status === RpcStatus.UNAVAILABLE;
        const canStream = outcome.compatibility === Compatibility.COMPATIBLE && helloAnswered;
        expect(firstStream >= 0).toBe(canStream);
        // Every call before the first stream is a Hello: failed ones, then the one that answered.
        if (canStream) expect(firstStream).toBe(outcome.failures + 1);
        else expect(calls.every(call => call.kind === 'unary')).toBe(true);
        if (outcome.compatibility !== Compatibility.COMPATIBLE && outcome.failures === 0) {
          // Incompatible: the answer is final until the user retries.
          expect(calls).toHaveLength(1);
          expect(freshness).toBe('unavailable');
        }
      }),
    {arbitrary: {runs: 24}},
  );
});

describe('an agent stream', () => {
  const agentFor = (daemon: FakeDaemon) => agentIdOf(daemon);
  const requestedAfter = (h: HostHarness) =>
    callsTo(h, 'WatchAgent').map(
      call => fromBinary(WatchAgentRequestSchema, call.target.request).afterSequence,
    );

  it.effect(
    'reads from the start when mounted, and from its cursor when reopened while mounted',
    () =>
      Effect.gen(function* () {
        const daemon = daemonAt();
        const id = agentFor(daemon);
        const h = yield* hostHarness(daemon, {
          // The first stream loses its link right after its frames.
          wrap: base => ({
            respond: (target, nth) =>
              target.method === methodPathOf('WatchAgent') && nth === 0
                ? base
                    .respond(target, nth)
                    .map(step => ('hang' in step ? {end: {status: RpcStatus.UNAVAILABLE}} : step))
                : base.respond(target, nth),
          }),
        });
        const unmount = h.mount(h.session.agents.watch(id));
        yield* h.elapse(60);
        const first = requestedAfter(h);
        expect(first[0]).toBe(0n);
        expect(first.length).toBeGreaterThan(1);
        const cursor = (yield* h.model).agentCursors[id] ?? 0n;
        expect(cursor).toBeGreaterThan(0n);
        expect(first[1]).toBeGreaterThan(0n);
        // A new mount reads the newest page again: the cursor never carries over.
        unmount();
        yield* h.elapse(1);
        h.mount(h.session.agents.watch(id));
        yield* h.elapse(1);
        expect(requestedAfter(h).at(-1)).toBe(0n);
      }),
  );

  it.effect('says it follows nothing once unmounted, and has no retry to release', () =>
    Effect.gen(function* () {
      const daemon = daemonAt();
      const id = agentFor(daemon);
      const h = yield* hostHarness(daemon);
      const unmount = h.mount(h.session.agents.watch(id));
      yield* h.elapse(1);
      expect(yield* h.read(h.session.agents.status(id))).toEqual({_tag: 'Live'});
      unmount();
      yield* h.elapse(1);
      expect(yield* h.read(h.session.agents.status(id))).toEqual({_tag: 'Opening'});
      expect(yield* h.read(h.session.agents.freshness(id))).not.toBe('live');
      yield* h.session.agents.retry(id);
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(0);
    }),
  );

  // P1 for an agent stream: its first open is a page and then the progress made since, a reopen
  // continues from the cursor. A link that drops anywhere, and a page or item frame delivered
  // twice, still end with the items and turns an uninterrupted stream gives. (A progress frame
  // carries no ordinal, so a repeated one cannot be told from new text: the transport delivers
  // each frame once, and the faults here repeat only frames whose repeat is a no-op.)
  describe('under generated faults (P1)', () => {
    const liveScenario = generateScenario({seed: 7, subagents: 2, live: true});
    const liveDaemon = () => new FakeDaemon(liveScenario);
    const watched = liveScenario.deltas[0]?.agentId ?? '';
    const progressFrames = liveScenario.deltas.filter(delta => delta.agentId === watched).length;

    const schedules = Schema.Struct({
      dropAfter: upTo(4, between(0, progressFrames + 6)),
      // The page (frame 0) and the frames after the progress: not the progress frames.
      duplicate: upTo(
        3,
        Schema.Union([Schema.Literal(0), between(progressFrames + 1, progressFrames + 6)]),
      ),
      helloFails: upTo(2, between(0, 3)),
    });

    const play = (schedule: typeof schedules.Type) =>
      Effect.scoped(
        Effect.gen(function* () {
          const failing = new Set(schedule.helloFails);
          const h = yield* hostHarness(liveDaemon(), {
            closeStreams: true,
            helloFails: nth => (failing.has(nth) ? RpcStatus.UNAVAILABLE : undefined),
            wrap: faultsOn('WatchAgent', {
              dropAfter: schedule.dropAfter,
              duplicate: schedule.duplicate,
            }),
          });
          h.mount(h.session.agents.watch(watched));
          yield* h.elapse(600, 5);
          return (yield* h.model).data;
        }).pipe(Random.withSeed(SEED)),
      );
    const textOf = (data: OrchestratorState) =>
      Object.fromEntries(Object.entries(data.overlays).map(([id, o]) => [id, o.text]));

    it.effect.prop(
      'ends with the items an uninterrupted stream gives, and progress that is a prefix of its',
      {schedule: schedules},
      ({schedule}) =>
        Effect.gen(function* () {
          const clean = yield* play({dropAfter: [], duplicate: [], helloFails: []});
          const faulty = yield* play(schedule);
          expect(faulty.items).toEqual(clean.items);
          expect(faulty.turns).toEqual(clean.turns);
          for (const [id, text] of Object.entries(textOf(faulty))) {
            expect(textOf(clean)[id]?.startsWith(text), `progress of ${id}`).toBe(true);
          }
        }),
      {arbitrary: {runs: 20}},
    );
  });

  it.effect('fails alone when the agent is unknown, and reopens on the user retry', () =>
    Effect.gen(function* () {
      const daemon = daemonAt();
      const id = agentFor(daemon);
      const h = yield* hostHarness(daemon, {
        wrap: base => ({
          respond: (target, nth) =>
            target.method === methodPathOf('WatchAgent') && nth === 0
              ? [{end: {message: 'no such agent', status: RpcStatus.NOT_FOUND}}]
              : base.respond(target, nth),
        }),
      });
      h.mount(h.session.runs.watch);
      h.mount(h.session.agents.watch(id));
      yield* h.elapse(30);
      const failed: FeedStatus = yield* h.read(h.session.agents.status(id));
      expect(failed).toMatchObject({_tag: 'Failed', fault: {kind: 'NotFound'}});
      expect(callsTo(h, 'WatchAgent')).toHaveLength(1);
      expect(callsTo(h, 'Hello')).toHaveLength(1);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      yield* h.session.agents.retry(id);
      yield* h.elapse(1);
      expect(callsTo(h, 'WatchAgent')).toHaveLength(2);
      expect(yield* h.read(h.session.agents.status(id))).toEqual({_tag: 'Live'});
    }),
  );
});

describe('open streams (P8)', () => {
  it.effect('are one per watched view, however many screens mount it', () =>
    Effect.gen(function* () {
      const daemon = daemonAt();
      const id = agentIdOf(daemon);
      const h = yield* hostHarness(daemon);
      const stops = [
        h.mount(h.session.runs.watch),
        h.mount(h.session.agents.watch(id)),
        h.mount(h.session.agents.watch(id)),
      ];
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(2);
      const [runs, firstAgent, secondAgent] = stops;
      firstAgent?.();
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(2);
      secondAgent?.();
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(1);
      runs?.();
      yield* h.elapse(1);
      expect(openStreams(h)).toHaveLength(0);
    }),
  );
});

/**
 * A host that restarts having lost the tail of its log (a power cut, or its state reset) tells a
 * client whose cursor is past the end to start again from a snapshot. The client reads from the
 * start and shows what the host holds, however much of the tail it lost; it never asks after the
 * same cursor again.
 */
describe('a host whose log ends before the cursor', () => {
  const requestedAfter = (h: HostHarness, method: 'WatchRuns' | 'WatchAgent') =>
    callsTo(h, method).map(call =>
      method === 'WatchRuns'
        ? fromBinary(WatchRunsRequestSchema, call.target.request).afterSequence
        : fromBinary(WatchAgentRequestSchema, call.target.request).afterSequence,
    );

  /** The first stream of `method` delivers its frames; then the host restarts, `lost` events short. */
  const restartLosing =
    (method: string, daemon: FakeDaemon, lost: number) =>
    (base: Recording): Recording => ({
      respond: (target, nth) => {
        const steps = base.respond(target, nth);
        if (target.method !== methodPathOf(method) || nth !== 0) return steps;
        daemon.loseTail(lost);
        return [
          ...steps.filter(step => 'frame' in step),
          {end: {message: 'the host restarted', status: RpcStatus.UNAVAILABLE}},
        ];
      },
    });

  const losses = {lost: between(1, TOTAL)};

  it.effect.prop(
    'the run stream reads a snapshot and shows what the host has',
    losses,
    ({lost}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = daemonAt();
          const h = yield* hostHarness(daemon, {wrap: restartLosing('WatchRuns', daemon, lost)});
          h.mount(h.session.runs.watch);
          yield* h.elapse(60, 5);
          expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
          expect(requestedAfter(h, 'WatchRuns')).toEqual([0n, BigInt(TOTAL), 0n]);
          const kept = runStreamShape({
            ...scenario.changes.slice(0, TOTAL - lost).reduce(applyChange, emptyState),
            summaries: Object.fromEntries(
              daemon.summaries().map(summary => [summary.runId, summary]),
            ),
          });
          expect(runStreamShape((yield* h.model).data)).toEqual(kept);
        }).pipe(Random.withSeed(SEED)),
      ),
    {arbitrary: {runs: 20}},
  );

  it.effect.prop(
    "an agent's stream reads its newest page again",
    losses,
    ({lost}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = daemonAt();
          const id = agentIdOf(daemon);
          const h = yield* hostHarness(daemon, {wrap: restartLosing('WatchAgent', daemon, lost)});
          h.mount(h.session.agents.watch(id));
          yield* h.elapse(60, 5);
          expect(yield* h.read(h.session.agents.status(id))).toEqual({_tag: 'Live'});
          expect(requestedAfter(h, 'WatchAgent')).toEqual([0n, BigInt(TOTAL), 0n]);
          expect((yield* h.model).agentCursors[id]).toBe(daemon.sequence);
        }).pipe(Random.withSeed(SEED)),
      ),
    {arbitrary: {runs: 20}},
  );

  /** The atoms of the stream `method` names, for the agent `id` when it is an agent's. */
  const feedOf = (h: HostHarness, method: 'WatchRuns' | 'WatchAgent', id: string) =>
    method === 'WatchRuns'
      ? {
          watch: h.session.runs.watch,
          status: h.session.runs.status,
          fresh: h.session.runs.freshness,
        }
      : {
          watch: h.session.agents.watch(id),
          status: h.session.agents.status(id),
          fresh: h.session.agents.freshness(id),
        };

  // What a stream read before stays on screen while it reads from the start again, so its freshness
  // says what is shown is being caught up with, or is out of date once that read is refused: never
  // that nothing has been read.
  it.effect.prop(
    'a stream that reads from the start again says what it holds is being caught up with',
    {
      ...losses,
      method: Schema.Literals(['WatchRuns', 'WatchAgent']),
      reread: Schema.Literals(['pending', 'refused']),
    },
    ({lost, method, reread}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = daemonAt();
          const id = agentIdOf(daemon);
          const restarted = restartLosing(method, daemon, lost);
          const h = yield* hostHarness(daemon, {
            // The read from the start, after the host refused the cursor, has not answered yet,
            // or is refused for good.
            wrap: base => {
              const played = restarted(base);
              return {
                respond: (target, nth) =>
                  target.method === methodPathOf(method) && nth >= 2
                    ? [
                        reread === 'pending'
                          ? {hang: true}
                          : {end: {message: 'no such stream', status: RpcStatus.NOT_FOUND}},
                      ]
                    : played.respond(target, nth),
              };
            },
          });
          const feed = feedOf(h, method, id);
          h.mount(feed.watch);
          yield* h.elapse(60, 5);
          expect(requestedAfter(h, method)).toEqual([0n, BigInt(TOTAL), 0n]);
          expect(yield* h.read(feed.status)).toMatchObject({
            _tag: reread === 'pending' ? 'Restarting' : 'Failed',
          });
          const {data} = yield* h.model;
          const held =
            method === 'WatchRuns'
              ? Object.keys(data.runs).length > 0
              : Object.values(data.items).some(item => item.agentId === id);
          expect(held).toBe(true);
          expect(yield* h.read(feed.fresh)).toBe(reread === 'pending' ? 'catching-up' : 'stale');
        }).pipe(Random.withSeed(SEED)),
      ),
    {arbitrary: {runs: 20}},
  );
});

/**
 * A run stream that reads a snapshot, then `inOrder - 1` events in order, then ends the way
 * `terminal` says: the next event skipped (`gap`), a reader too slow for the stream (`overflow`), a
 * clean close by the host (`close`). The first `passes` opens play this and later ones play the
 * daemon's own answer, so a test can see how many opens a misbehaving stream earns.
 */
describe('a stream that keeps ending', () => {
  type Terminal = 'gap' | 'overflow' | 'close';
  const TERMINALS: ReadonlyArray<Terminal> = ['gap', 'overflow', 'close'];
  const empty = new FakeDaemon(scenario, 0);
  const encode = (body: Parameters<typeof create<typeof WatchRunsResponseSchema>>[1]): Step => ({
    frame: toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, body)),
  });

  /** The frames of one misbehaving pass: `inOrder` frames in order, then the way it ends. */
  const passSteps = (inOrder: number, terminal: Terminal): ReadonlyArray<Step> => {
    const steps: Step[] = [encode({body: {case: 'snapshot', value: empty.snapshot()}})];
    for (const event of scenario.changes.slice(0, inOrder - 1)) {
      steps.push(encode({body: {case: 'change', value: event}}));
    }
    const skipped = scenario.changes[inOrder];
    if (terminal === 'gap' && skipped !== undefined) {
      return [...steps, encode({body: {case: 'change', value: skipped}}), {hang: true}];
    }
    return [...steps, terminal === 'overflow' ? {overflow: true} : {close: true}];
  };

  const shaped =
    (inOrder: number, terminal: Terminal, passes: number) =>
    (base: Recording): Recording => ({
      respond: (target, nth) =>
        target.method === methodPathOf('WatchRuns') && nth < passes
          ? passSteps(inOrder, terminal)
          : base.respond(target, nth),
    });

  const opens = (h: HostHarness) => callsTo(h, 'WatchRuns').length;

  /** The most dials the reconnect policy allows in `seconds`, taking its shortest wait each time. */
  const mostDialsIn = (seconds: number): number => {
    let waited = 0;
    let dials = 1;
    while (waited + delayMs(defaultReconnect, dials, 0) <= seconds * 1000) {
      waited += delayMs(defaultReconnect, dials, 0);
      dials += 1;
    }
    return dials;
  };

  describe('before it delivers', () => {
    const pass = it.effect.prop(
      'is opened at most MAX_QUIET_RESTARTS + 1 times per connection, and the connection backs off between',
      {
        inOrder: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: STABLE_PASS_FRAMES - 1})),
        terminal: Schema.Literals(TERMINALS),
      },
      ({inOrder, terminal}) =>
        Effect.scoped(
          Effect.gen(function* () {
            const h = yield* hostHarness(daemonAt(), {
              wrap: shaped(inOrder, terminal, Number.POSITIVE_INFINITY),
            });
            h.mount(h.session.runs.watch);
            yield* h.elapse(0);
            // The first connection: four opens, no more, and the link is not yet blamed.
            expect(opens(h)).toBe(MAX_QUIET_RESTARTS + 1);
            expect(callsTo(h, 'Hello')).toHaveLength(1);
            // Blamed once, the connection waits (a longer wait each time) before the next dial.
            yield* h.elapse(300, 5);
            const epochs = callsTo(h, 'Hello').length;
            expect(epochs).toBeGreaterThan(1);
            expect(epochs).toBeLessThanOrEqual(mostDialsIn(300));
            expect(opens(h)).toBeLessThanOrEqual((MAX_QUIET_RESTARTS + 1) * epochs);
          }),
        ),
      {arbitrary: {runs: 20}},
    );
    return pass;
  });

  describe('after a healthy pass', () => {
    it.effect.prop(
      'is reopened without blaming the connection, however many times it ends',
      {
        inOrder: Schema.Int.check(
          Schema.isBetween({minimum: STABLE_PASS_FRAMES, maximum: STABLE_PASS_FRAMES + 3}),
        ),
        terminal: Schema.Literals(TERMINALS),
      },
      ({inOrder, terminal}) =>
        Effect.scoped(
          Effect.gen(function* () {
            const passes = 3 * (MAX_QUIET_RESTARTS + 1);
            const h = yield* hostHarness(daemonAt(), {wrap: shaped(inOrder, terminal, passes)});
            h.mount(h.session.runs.watch);
            yield* h.elapse(0);
            expect(opens(h)).toBeGreaterThan(passes);
            expect(callsTo(h, 'Hello')).toHaveLength(1);
            expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
          }),
        ),
      {arbitrary: {runs: 20}},
    );
  });

  it.effect('a healthy pass between quiet ones starts the count over', () =>
    Effect.gen(function* () {
      // Opens 0-2 and 4-6 end quiet; open 3 is healthy. Six quiet ends would blame the link
      // if the healthy one did not start the count over. Open 7 and later are the daemon's own.
      const quiet = shaped(1, 'gap', Number.POSITIVE_INFINITY);
      const healthy = shaped(STABLE_PASS_FRAMES, 'gap', Number.POSITIVE_INFINITY);
      const h = yield* hostHarness(daemonAt(), {
        wrap: base => ({
          respond: (target, nth) => {
            if (target.method !== methodPathOf('WatchRuns') || nth >= 7) {
              return base.respond(target, nth);
            }
            return (nth === 3 ? healthy : quiet)(base).respond(target, nth);
          },
        }),
      });
      h.mount(h.session.runs.watch);
      yield* h.elapse(0);
      expect(opens(h)).toBeGreaterThan(7);
      expect(callsTo(h, 'Hello')).toHaveLength(1);
    }),
  );

  it.effect('a clean close of one stream reopens that stream, not the connection', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(daemonAt(), {wrap: shaped(1, 'close', 1)});
      h.mount(h.session.runs.watch);
      yield* h.elapse(1);
      expect(opens(h)).toBe(2);
      expect(callsTo(h, 'Hello')).toHaveLength(1);
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
    }),
  );

  it.effect('an agent method the host lacks fails that feed alone and leaves the link up', () =>
    Effect.gen(function* () {
      const daemon = daemonAt();
      const id = agentIdOf(daemon);
      const h = yield* hostHarness(daemon, {
        wrap: base => ({
          respond: (target, nth) =>
            target.method === methodPathOf('WatchAgent')
              ? [{end: {message: 'no such method', status: RpcStatus.UNIMPLEMENTED}}]
              : base.respond(target, nth),
        }),
      });
      h.mount(h.session.runs.watch);
      h.mount(h.session.agents.watch(id));
      yield* h.elapse(60);
      expect(yield* h.read(h.session.agents.status(id))).toMatchObject({
        _tag: 'Failed',
        fault: {kind: 'Unimplemented'},
      });
      expect(yield* h.read(h.session.runs.status)).toEqual({_tag: 'Live'});
      expect(callsTo(h, 'Hello')).toHaveLength(1);
      expect(yield* h.read(h.session.runs.freshness)).toBe('live');
    }),
  );
});

it.effect(
  'a resumed stream replaces revision 5 with a new boot revision 1 without changing the cursor',
  () =>
    Effect.gen(function* () {
      const encode = (search: ReturnType<typeof create<typeof SearchStateSchema>>): Step => ({
        frame: toBinary(
          WatchRunsResponseSchema,
          create(WatchRunsResponseSchema, {body: {case: 'search', value: search}}),
        ),
      });
      const first = create(SearchStateSchema, {
        runId: scenario.runId,
        revision: 5n,
        unattributedJobs: 5,
        health: {
          state: {
            case: 'unreadable',
            value: {reason: UnreadableReason.NO_DAEMON, since: {seconds: 1n, nanos: 0}},
          },
        },
      });
      const next = create(SearchStateSchema, {
        runId: scenario.runId,
        revision: 1n,
        unattributedJobs: 1,
        health: {
          state: {
            case: 'unreadable',
            value: {reason: UnreadableReason.NO_DAEMON, since: {seconds: 1n, nanos: 0}},
          },
        },
      });
      const last = scenario.changes.at(-1);
      if (last === undefined) throw new Error('scenario must have an event');
      const cursors: bigint[] = [];
      const h = yield* hostHarness(daemonAt(TOTAL - 1), {
        wrap: base => ({
          respond: (target, nth) => {
            if (target.method !== methodPathOf('WatchRuns')) return base.respond(target, nth);
            if (nth === 0)
              return [
                ...base.respond(target, nth).filter(step => 'frame' in step),
                encode(first),
                {
                  frame: toBinary(
                    WatchRunsResponseSchema,
                    create(WatchRunsResponseSchema, {body: {case: 'change', value: last}}),
                  ),
                },
                {close: true},
              ];
            cursors.push(fromBinary(WatchRunsRequestSchema, target.request).afterSequence);
            return [encode(next), {hang: true}];
          },
        }),
      });
      h.mount(h.session.runs.watch);
      const data = (yield* h.model).data;
      expect(cursors).toEqual([BigInt(TOTAL)]);
      expect(data.searches[scenario.runId]).toEqual(next);
      expect(data.sequence).toBe(BigInt(TOTAL));
      expect(data.gap).toBeNull();
    }),
);

it.effect('the Fake sends current search frames on resume after replayed events', () =>
  Effect.gen(function* () {
    const daemon = new FakeDaemon({...scenario, search: true});
    const resumed: {cursor: bigint; cases: unknown[]}[] = [];
    const h = yield* hostHarness(daemon, {
      wrap: base => ({
        respond: (target, nth) => {
          const steps = base.respond(target, nth);
          if (target.method !== methodPathOf('WatchRuns')) return steps;
          if (nth === 0) return [...steps.filter(step => 'frame' in step), {close: true}];
          const frames = steps.flatMap(step =>
            'frame' in step ? [fromBinary(WatchRunsResponseSchema, step.frame)] : [],
          );
          resumed.push({
            cursor: fromBinary(WatchRunsRequestSchema, target.request).afterSequence,
            cases: frames.map(frame => frame.body.case),
          });
          return steps;
        },
      }),
    });
    h.mount(h.session.runs.watch);
    const data = (yield* h.model).data;
    // The host sends a stream that resumed the search view and the summary of each run, which it holds none of.
    expect(resumed).toEqual([{cursor: daemon.sequence, cases: ['search', 'summary']}]);
    expect(data.searches[scenario.runId]).toEqual(daemon.snapshot().searches[0]);
    expect(callsTo(h, 'WatchRuns')).toHaveLength(2);
    expect(data.gap).toBeNull();
  }),
);

it.effect('opening a feed without begin publishes no unchanged model', () =>
  Effect.gen(function* () {
    const model = yield* SubscriptionRef.make(emptyHostModel);
    const initial = yield* Deferred.make<void>();
    const opened = yield* Deferred.make<void>();
    const publications: HostModel[] = [];
    const retry = Effect.void;
    yield* SubscriptionRef.changes(model).pipe(
      Stream.runForEach(value =>
        Effect.gen(function* () {
          publications.push(value);
          yield* Deferred.succeed(initial, undefined);
        }),
      ),
      Effect.forkScoped,
    );
    yield* Deferred.await(initial);
    yield* runFeed(
      {
        model,
        connection: {
          state: Atom.make({_tag: 'Idle'} as const),
          freshness: Atom.make({status: 'stale', lastUpdateMs: null} as const),
          heard: Effect.void,
          ready: () => Effect.succeed(1),
          suspect: () => Effect.void,
          blocked: Effect.succeed(undefined),
          retry,
          leftTerminal: Effect.never,
        },
        setStatus: () => Effect.void,
        awaitRetry: () => Effect.never,
      },
      {
        open: () =>
          Stream.fromEffect(Deferred.succeed(opened, undefined)).pipe(
            Stream.flatMap(() => Stream.never),
          ),
        apply: value => value,
        gapped: () => false,
      },
    ).pipe(Effect.forkScoped);
    yield* Deferred.await(opened);
    expect(publications).toEqual([emptyHostModel]);
  }),
);
