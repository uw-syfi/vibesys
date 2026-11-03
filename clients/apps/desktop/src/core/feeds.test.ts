import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  Compatibility,
  WatchAgentRequestSchema,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import fc from 'fast-check';
import {
  applyEvent,
  emptyState,
  type FeedStatus,
  type Freshness,
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

const SEED = 20261103;
const scenario = generateScenario({seed: 7, subagents: 2, live: false});
const TOTAL = scenario.events.length;
const daemonAt = (visible: number = TOTAL) => new FakeDaemon(scenario, visible);

/** What the run stream defines of the state: a snapshot and the events after it agree on it. */
const runStreamShape = (s: OrchestratorState) => ({
  sequence: s.sequence,
  runs: s.runs,
  agents: s.agents,
  turns: s.turns,
  gap: s.gap,
});
const everything = runStreamShape(scenario.events.reduce(applyEvent, emptyState));

const agentIdOf = (daemon: FakeDaemon): string => {
  const [agent] = daemon.snapshot().agents;
  if (agent === undefined) throw new Error('the scenario has no agent');
  return agent.id;
};

const callsTo = (h: HostHarness, method: string) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf(method));
const openStreams = (h: HostHarness) =>
  h.fake.calls().filter(call => call.kind === 'stream' && call.open);

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

  // P1: a stream interrupted by any link faults ends with the state an uninterrupted one has.
  describe('under generated faults (P1)', () => {
    interface Schedule {
      readonly startTenths: number;
      readonly commits: ReadonlyArray<number>;
      readonly plan: FaultPlan;
      readonly helloFails: ReadonlyArray<number>;
    }
    const schedules = fc.record({
      startTenths: fc.integer({min: 1, max: 9}),
      commits: fc.array(fc.integer({min: 1, max: 60}), {maxLength: 3}),
      plan: fc.record({
        dropAfter: fc.array(fc.integer({min: 0, max: 6}), {maxLength: 4}),
        duplicate: fc.array(fc.integer({min: 0, max: 5}), {maxLength: 3}),
      }),
      helloFails: fc.array(fc.integer({min: 0, max: 3}), {maxLength: 2}),
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

    it.effect('ends in the state of an uninterrupted stream', () =>
      Effect.gen(function* () {
        for (const schedule of fc.sample(schedules, {seed: SEED, numRuns: 20})) {
          expect(yield* play(schedule).pipe(Random.withSeed(SEED))).toEqual(everything);
        }
      }),
    );
  });

  describe('a gap in the stream', () => {
    const START = Math.floor(TOTAL / 2);
    const skippedEvent = scenario.events[START + 1];

    /** The frame of an event two ahead of the snapshot, so one event is missing. */
    const gapFrame = (): Step | undefined =>
      skippedEvent === undefined
        ? undefined
        : {
            frame: toBinary(
              WatchRunsResponseSchema,
              create(WatchRunsResponseSchema, {body: {case: 'event', value: skippedEvent}}),
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
  const outcomes = fc.record({
    compatibility: fc.constantFrom(
      Compatibility.COMPATIBLE,
      Compatibility.CLIENT_TOO_OLD,
      Compatibility.DAEMON_TOO_OLD,
    ),
    failures: fc.integer({min: 0, max: 3}),
    status: fc.constantFrom(RpcStatus.UNAVAILABLE, RpcStatus.UNIMPLEMENTED),
  });

  type Outcome = typeof outcomes extends fc.Arbitrary<infer T> ? T : never;
  const sampleOutcomes = () => fc.sample(outcomes, {seed: SEED, numRuns: 24});

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

  it.effect('opens no stream until one has been answered COMPATIBLE', () =>
    Effect.gen(function* () {
      for (const outcome of sampleOutcomes()) {
        const {h, freshness} = yield* play(outcome);
        const calls = h.fake.calls();
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
      }
    }),
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
