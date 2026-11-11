import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {RunLiveness, SubscribeRequestSchema, TopicFailureCode, topicPolicies} from '@vibesys/proto';
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
