import {fromBinary, type MessageInitShape} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  Direction,
  DrainGoal,
  type LifecycleSchema,
  Outcome,
  outcomePresentation,
  RunLiveness,
  SeriesRole,
  StageState,
  StartupStage,
  SubscribeRequestSchema,
  TopicFailureCode,
  topicPolicies,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import {statsFrame, TWO_GPUS} from '../testing/machine-stats.js';
import {
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import {
  RUNS_SUBSCRIBE,
  runEntryFrame,
  runRemovedFrame,
  runsHeartbeatFrame,
  runsRefusedFrame,
  runsSyncedFrame,
  servingRuns,
} from '../testing/runs-topic.js';
import type {Step} from '../testing/transport/index.js';
import {RpcStatus} from './errors.js';

const scenario = generateScenario({seed: 19, subagents: 0, live: false});

const harness = (streams: (nth: number) => ReadonlyArray<Step>) =>
  hostHarness(new FakeDaemon(scenario), {wrap: servingRuns(streams)});

const topicCalls = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === RUNS_SUBSCRIBE);
const hellos = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('Hello'));

const read = (h: HostHarness) => h.read(h.session.runIndex.state);

describe('the runs topic feed', () => {
  it.effect('projects only typed RunEntry and RunHead facts for the renderer', () =>
    Effect.gen(function* () {
      const h = yield* harness(() => [
        runEntryFrame('generic-run', {
          liveness: RunLiveness.ALIVE,
          projectRoot: '/projects/serve',
          head: {
            strategy: 'standard',
            lifecycle: {state: {case: 'running', value: {}}},
            objective: {
              series: {
                name: 'Throughput',
                unit: 'requests/s',
                direction: Direction.MAX,
                role: SeriesRole.OBJECTIVE,
              },
              bestValue: 87.5,
            },
            usage: {costUsd: 4.25},
            needsUser: true,
          },
        }),
        runsSyncedFrame,
        {hang: true},
      ]);
      h.mount(h.session.runIndex.watch);
      yield* h.elapse(0);
      const index = yield* h.read(h.session.runIndex.index);
      expect(index).toMatchObject({_tag: 'Ready', freshness: 'current'});
      if (index._tag !== 'Ready') return;
      expect(index.runs.get('generic-run')).toEqual({
        runId: 'generic-run',
        projectRoot: '/projects/serve',
        state: 'running',
        activity: 'live',
        objective: {name: 'Throughput', unit: 'requests/s', bestValue: 87.5},
        costUsd: 4.25,
        needsUser: true,
      });
    }),
  );

  it.effect('keeps persisted terminal outcomes authoritative over lock-derived liveness', () =>
    Effect.gen(function* () {
      const ended = (outcome: Outcome, liveness = RunLiveness.ENDED) => ({
        liveness,
        head: {
          strategy: 'standard',
          lifecycle: {
            state: {
              case: 'ended' as const,
              value: {outcome, presentation: outcomePresentation(outcome)},
            },
          },
        },
      });
      const h = yield* harness(() => [
        runEntryFrame('interrupted', ended(Outcome.INTERRUPTED)),
        runEntryFrame('persisted-stopped', ended(Outcome.STOPPED, RunLiveness.INTERRUPTED)),
        runsSyncedFrame,
        {hang: true},
      ]);
      h.mount(h.session.runIndex.watch);
      yield* h.elapse(0);
      const index = yield* h.read(h.session.runIndex.index);
      expect(index._tag).toBe('Ready');
      if (index._tag !== 'Ready') return;
      expect(index.runs.get('interrupted')).toMatchObject({
        state: 'interrupted',
        activity: 'ended',
      });
      expect(index.runs.get('persisted-stopped')).toMatchObject({
        state: 'stopped',
        activity: 'ended',
      });
    }),
  );

  it.effect('lets lock-derived interruption override every stale nonterminal head', () =>
    Effect.gen(function* () {
      const head = (state: MessageInitShape<typeof LifecycleSchema>) => ({
        liveness: RunLiveness.INTERRUPTED,
        head: {strategy: 'standard', lifecycle: state},
      });
      const h = yield* harness(() => [
        runEntryFrame('awaiting', {
          liveness: RunLiveness.INTERRUPTED,
          head: {
            strategy: 'standard',
            awaitingLifecycle: true,
            epoch: 1,
            committedSeq: 0n,
          },
        }),
        runEntryFrame(
          'starting',
          head({
            state: {
              case: 'starting',
              value: {stage: StartupStage.PROBING_WORKSPACE, state: StageState.RUNNING},
            },
          }),
        ),
        runEntryFrame('running', head({state: {case: 'running', value: {}}})),
        runEntryFrame('draining', head({state: {case: 'draining', value: {goal: DrainGoal.STOP}}})),
        runsSyncedFrame,
        {hang: true},
      ]);
      h.mount(h.session.runIndex.watch);
      yield* h.elapse(0);
      const index = yield* h.read(h.session.runIndex.index);
      expect(index._tag).toBe('Ready');
      if (index._tag !== 'Ready') return;
      expect([...index.runs.values()].map(run => [run.runId, run.state, run.activity])).toEqual([
        ['awaiting', 'interrupted', 'ended'],
        ['starting', 'interrupted', 'ended'],
        ['running', 'interrupted', 'ended'],
        ['draining', 'interrupted', 'ended'],
      ]);
    }),
  );

  it.effect('preserves held, parking and stopping lifecycle intent', () =>
    Effect.gen(function* () {
      const h = yield* harness(() => [
        runEntryFrame('held', {
          liveness: RunLiveness.ALIVE,
          head: {
            strategy: 'standard',
            lifecycle: {
              state: {
                case: 'running',
                value: {hold: {reason: {case: 'operator', value: {}}}},
              },
            },
          },
        }),
        runEntryFrame('parking', {
          liveness: RunLiveness.ALIVE,
          head: {
            strategy: 'standard',
            lifecycle: {state: {case: 'draining', value: {goal: DrainGoal.PARK}}},
          },
        }),
        runEntryFrame('stopping', {
          liveness: RunLiveness.ALIVE,
          head: {
            strategy: 'standard',
            lifecycle: {state: {case: 'draining', value: {goal: DrainGoal.STOP}}},
          },
        }),
        runsSyncedFrame,
        {hang: true},
      ]);
      h.mount(h.session.runIndex.watch);
      yield* h.elapse(0);
      const index = yield* h.read(h.session.runIndex.index);
      expect(index._tag).toBe('Ready');
      if (index._tag !== 'Ready') return;
      expect([...index.runs.values()].map(run => [run.runId, run.state, run.activity])).toEqual([
        ['held', 'held', 'live'],
        ['parking', 'parking', 'live'],
        ['stopping', 'stopping', 'live'],
      ]);
    }),
  );

  it.effect('mounts and releases both disjoint discovery streams as one navigation lifetime', () =>
    Effect.gen(function* () {
      const h = yield* harness(() => [runsSyncedFrame, {hang: true}]);
      const unmount = h.mount(h.session.runNavigation);
      yield* h.elapse(0);
      const methods = h.fake.calls().map(call => call.target.method);
      expect(methods).toContain(RUNS_SUBSCRIBE);
      expect(methods).toContain(methodPathOf('WatchRuns'));
      unmount();
      yield* h.elapse(0);
      const discovery = h.fake
        .calls()
        .filter(call => [RUNS_SUBSCRIBE, methodPathOf('WatchRuns')].includes(call.target.method));
      expect(discovery.every(call => call.open === false)).toBe(true);
      expect(yield* h.read(h.session.runIndex.index)).toEqual({_tag: 'Dormant'});
    }),
  );

  it.effect('reads an atomic multi-key snapshot and removals only while mounted', () =>
    Effect.gen(function* () {
      const h = yield* harness(() => [
        runEntryFrame('run-a', {liveness: RunLiveness.ALIVE}),
        {sleep: 1000},
        runEntryFrame('run-b', {liveness: RunLiveness.ENDED}),
        {sleep: 1000},
        runsSyncedFrame,
        {sleep: 1000},
        runRemovedFrame('run-a'),
        {hang: true},
      ]);
      expect(yield* read(h)).toEqual({_tag: 'Dormant'});
      const unmount = h.mount(h.session.runIndex.watch);
      yield* h.elapse(0);
      const [call] = topicCalls(h);
      expect(call && fromBinary(SubscribeRequestSchema, call.target.request).topic.case).toBe(
        'runs',
      );
      expect(yield* read(h)).toMatchObject({_tag: 'Connecting'});
      yield* h.elapse(1);
      expect(yield* read(h)).toMatchObject({_tag: 'Connecting'});
      yield* h.elapse(1);
      const live = yield* read(h);
      expect(live._tag).toBe('Live');
      if (live._tag === 'Live') {
        expect([...live.value.keys()]).toEqual(['run-a', 'run-b']);
      }
      yield* h.elapse(1);
      const changed = yield* read(h);
      expect(changed._tag).toBe('Live');
      if (changed._tag === 'Live') expect([...changed.value.keys()]).toEqual(['run-b']);
      unmount();
      yield* h.elapse(0);
      expect(yield* read(h)).toEqual({_tag: 'Dormant'});
      expect(topicCalls(h).map(call => call.open)).toEqual([false]);
    }),
  );

  it.effect('keeps a stale map through reconnect and atomically replaces it without ghosts', () =>
    Effect.gen(function* () {
      const h = yield* harness(nth =>
        nth === 0
          ? [
              runEntryFrame('old-run'),
              runsSyncedFrame,
              {sleep: 5000},
              {end: {status: RpcStatus.UNAVAILABLE}},
            ]
          : [runEntryFrame('new-run'), runsSyncedFrame, {hang: true}],
      );
      h.mount(h.session.runIndex.watch);
      yield* h.elapse(0);
      expect(yield* read(h)).toMatchObject({_tag: 'Live'});
      yield* h.elapse(5);
      const stale = yield* read(h);
      expect(stale).toMatchObject({_tag: 'Stale', why: 'disconnected'});
      if (stale._tag === 'Stale') {
        expect(stale.value === null ? [] : [...stale.value.keys()]).toEqual(['old-run']);
      }
      yield* h.elapse(5);
      const recovered = yield* read(h);
      expect(recovered._tag).toBe('Live');
      if (recovered._tag === 'Live') expect([...recovered.value.keys()]).toEqual(['new-run']);
      expect(hellos(h)).toHaveLength(2);
      expect(topicCalls(h)).toHaveLength(2);
    }),
  );

  it.effect('keeps typed refusals distinct from invalid frames', () =>
    Effect.gen(function* () {
      const refused = yield* harness(() => [runsRefusedFrame(TopicFailureCode.UNSUPPORTED)]);
      refused.mount(refused.session.runIndex.watch);
      yield* refused.elapse(0);
      expect(yield* read(refused)).toEqual({_tag: 'Refused', refusal: 'unsupported'});
      yield* refused.elapse(60);
      expect(topicCalls(refused)).toHaveLength(1);

      const invalid = yield* harness(() => [statsFrame(TWO_GPUS)]);
      invalid.mount(invalid.session.runIndex.watch);
      yield* invalid.elapse(0);
      expect(yield* read(invalid)).toEqual({
        _tag: 'Failed',
        failure: {
          _tag: 'InvalidFrame',
          detail: 'an entry of the machine_stats topic',
        },
      });
    }),
  );

  it.effect('bounds a snapshot that sends only heartbeats, then recovers on a new stream', () =>
    Effect.gen(function* () {
      const beats = Array.from({length: 8}, () => [{sleep: 5000}, runsHeartbeatFrame]).flat();
      const h = yield* harness(nth =>
        nth === 0
          ? [...beats, {hang: true}]
          : [runEntryFrame('run-a'), runsSyncedFrame, {hang: true}],
      );
      h.mount(h.session.runIndex.watch);
      yield* h.elapse(topicPolicies.runs.firstFrameMs / 1000 - 1);
      expect(yield* read(h)).toMatchObject({_tag: 'Connecting'});
      yield* h.elapse(1);
      expect(yield* read(h)).toMatchObject({_tag: 'Stale', value: null, why: 'silent'});
      yield* h.elapse(1);
      expect(yield* read(h)).toMatchObject({_tag: 'Live'});
      expect(topicCalls(h)).toHaveLength(2);
    }),
  );
});
