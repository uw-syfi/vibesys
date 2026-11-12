import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {SkeletonRequestSchema, TailRequestSchema} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import {
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
} from '../../testing/orchestrator/index.js';
import {
  rawSkeleton,
  representativeRun,
  runHeader,
  runMetric,
  SKELETON,
  servingRunDetail,
  skeleton,
  TAIL,
  tail,
} from '../../testing/run-detail/index.js';
import type {Step} from '../../testing/transport/index.js';
import {RpcStatus} from '../errors.js';

const RUN = 'run-dynamic';
const scenario = generateScenario({seed: 23, subagents: 0, live: false});

const harness = (
  skeletons: (nth: number) => ReadonlyArray<Step>,
  tails: (nth: number) => ReadonlyArray<Step>,
) => hostHarness(new FakeDaemon(scenario), {wrap: servingRunDetail(skeletons, tails)});

type Harness = HostHarness;

const calls = (h: Harness, method: string) =>
  h.fake.calls().filter(call => call.target.method === method);

describe('the generic run detail feed', () => {
  it.effect('replays one representative recorded run through the public typed service', () =>
    Effect.gen(function* () {
      const recorded = representativeRun(RUN);
      const h = yield* harness(
        () => [skeleton(recorded.prefix, recorded.committedSequence)],
        () => [tail(recorded.continuation), {hang: true}],
      );
      h.mount(h.session.runDetail.watch(RUN));
      const state = yield* h.read(h.session.runDetail.state(RUN));
      expect(state._tag).toBe('Live');
      if (state._tag !== 'Live') return;

      expect(state.projection.freshness).toEqual({
        kind: 'current',
        throughSequence: recorded.committedSequence,
      });
      expect(state.projection.agents).toMatchObject({
        availability: {kind: 'available'},
        values: [
          {
            agentId: 'agent:dynamic',
            role: recorded.dynamicRole,
            model: 'model-x',
            provider: 'provider-y',
            providerSessionIds: ['session-open'],
            source: {kind: 'run-record', sequence: 2n, epoch: 1},
            scope: {kind: 'span', runId: RUN, spanId: 'agent-dynamic'},
          },
        ],
      });
      expect(state.projection.workstreams.values).toMatchObject([
        {
          spanId: 'work-root',
          hypothesisId: 'hypothesis-a',
          parentRevision: 'revision-base',
          source: {sequence: 3n, epoch: 1},
        },
      ]);
      expect(state.projection.correctness.values).toMatchObject([
        {
          spanId: 'gate-root',
          stage: 'measurement',
          subject: 'candidate',
          failedCheck: {check: 'correctness', completed: 93n, required: 100n, unit: 'trials'},
          frameworkSource: 'gpu',
          source: {kind: 'run-record', sequence: 4n, epoch: 1},
          scope: {kind: 'span', runId: RUN, spanId: 'gate-root'},
        },
      ]);
      expect(state.projection.unprojectedSpans).toMatchObject({
        availability: {kind: 'available'},
        values: [
          {
            spanId: 'opaque-root',
            reason: 'descriptor-unknown',
            source: {kind: 'run-record', sequence: 5n, epoch: 1},
          },
        ],
      });

      const throughput = state.projection.metrics.values.find(
        series => series.name === 'throughput',
      );
      expect(throughput).toMatchObject({
        unit: 'items/s',
        role: 'objective',
        omittedPoints: 1,
        scope: {kind: 'run', runId: RUN},
      });
      expect(throughput?.points).toHaveLength(512);
      expect(throughput?.points.at(0)?.value).toBe(1);
      expect(throughput?.points.at(-1)?.value).toBe(512);
      expect(state.projection.metrics.availability).toEqual({
        kind: 'partial',
        reasons: ['state-truncated'],
      });
      expect(state.projection.metrics.values).toContainEqual(
        expect.objectContaining({
          name: 'gpu_utilization',
          unit: 'percent',
          role: 'resource',
          source: {kind: 'run-record', sequence: 1n, epoch: 1},
          scope: {kind: 'run', runId: RUN},
          points: [
            expect.objectContaining({
              value: 75,
              source: {kind: 'run-record', sequence: recorded.committedSequence, epoch: 1},
              scope: {kind: 'run', runId: RUN},
            }),
          ],
        }),
      );
    }),
  );

  it.effect('acquires Skeleton, tails from next_seq, and releases the Tail on interruption', () =>
    Effect.gen(function* () {
      const h = yield* harness(
        () => [skeleton([runHeader(RUN)], 2n)],
        () => [tail([runMetric(2n, 12)]), {hang: true}],
      );
      expect(yield* h.read(h.session.runDetail.state(RUN))).toEqual({_tag: 'Loading'});
      const unmount = h.mount(h.session.runDetail.watch(RUN));
      const state = yield* h.read(h.session.runDetail.state(RUN));
      expect(state._tag).toBe('Live');
      if (state._tag === 'Live') {
        expect(state.projection.metrics.values).toMatchObject([
          {name: 'throughput', unit: 'items/s', points: [{value: 12}]},
        ]);
      }
      const [skeletonCall] = calls(h, SKELETON);
      expect(
        skeletonCall &&
          fromBinary(SkeletonRequestSchema, skeletonCall.target.request).knownDescriptorsSha256,
      ).toEqual([]);
      const [tailCall] = calls(h, TAIL);
      expect(tailCall && fromBinary(TailRequestSchema, tailCall.target.request)).toMatchObject({
        runId: RUN,
        fromSeq: 2n,
        sessionId: '',
      });
      expect(tailCall?.open).toBe(true);
      unmount();
      yield* h.elapse(0);
      expect(calls(h, TAIL)[0]).toMatchObject({open: false, released: 1});
      expect(yield* h.read(h.session.runDetail.state(RUN))).toEqual({_tag: 'Loading'});
    }),
  );

  it.effect('repairs a reducer gap with a new Skeleton before opening another Tail', () =>
    Effect.gen(function* () {
      const third = runMetric(3n, 3);
      const records = [runHeader(RUN), runMetric(2n, 2), third];
      const h = yield* harness(
        nth => (nth === 0 ? [skeleton(records.slice(0, 1), 3n)] : [skeleton(records)]),
        nth => (nth === 0 ? [tail([third]), {hang: true}] : [{hang: true}]),
      );
      h.mount(h.session.runDetail.watch(RUN));
      const state = yield* h.read(h.session.runDetail.state(RUN));
      expect(state._tag).toBe('Live');
      if (state._tag === 'Live') {
        expect(state.projection.metrics.values[0]?.points.map(point => point.value)).toEqual([
          2, 3,
        ]);
      }
      expect(calls(h, SKELETON)).toHaveLength(2);
      expect(
        calls(h, TAIL).map(call => fromBinary(TailRequestSchema, call.target.request).fromSeq),
      ).toEqual([2n, 4n]);
      expect(calls(h, TAIL)[0]).toMatchObject({open: false, released: 1});
    }),
  );

  it.effect('refreshes a rejected Skeleton and bounds repeated invalid snapshots', () =>
    Effect.gen(function* () {
      const invalid = rawSkeleton({committedSeq: 1n, nextSeq: 1n, complete: true});
      const recovered = yield* harness(
        nth => (nth === 0 ? [invalid] : [skeleton([runHeader(RUN)])]),
        () => [{hang: true}],
      );
      recovered.mount(recovered.session.runDetail.watch(RUN));
      expect(yield* recovered.read(recovered.session.runDetail.state(RUN))).toMatchObject({
        _tag: 'Live',
      });
      expect(calls(recovered, SKELETON)).toHaveLength(2);

      const broken = yield* harness(
        () => [invalid],
        () => [{hang: true}],
      );
      broken.mount(broken.session.runDetail.watch(RUN));
      expect(yield* broken.read(broken.session.runDetail.state(RUN))).toMatchObject({
        _tag: 'Failed',
        fault: {kind: 'InvalidMessage', retryable: false},
        projection: null,
      });
      expect(calls(broken, SKELETON)).toHaveLength(4);
      expect(calls(broken, TAIL)).toHaveLength(0);
    }),
  );

  it.effect('keeps the last valid projection with a refusal and retries only when asked', () =>
    Effect.gen(function* () {
      const h = yield* harness(
        () => [skeleton([runHeader(RUN)])],
        nth =>
          nth === 0
            ? [{end: {status: RpcStatus.UNIMPLEMENTED, message: 'tail is unavailable'}}]
            : [{hang: true}],
      );
      h.mount(h.session.runDetail.watch(RUN));
      const refused = yield* h.read(h.session.runDetail.state(RUN));
      expect(refused).toMatchObject({
        _tag: 'Refused',
        fault: {kind: 'Unimplemented', retryable: false},
        projection: {runId: RUN},
      });
      expect(calls(h, SKELETON)).toHaveLength(1);
      yield* h.session.runDetail.retry(RUN);
      expect(yield* h.read(h.session.runDetail.state(RUN))).toMatchObject({_tag: 'Live'});
      expect(calls(h, SKELETON)).toHaveLength(2);
      expect(calls(h, TAIL)).toHaveLength(2);
    }),
  );

  it.effect('marks a lost Tail stale and reacquires Skeleton after connection backoff', () =>
    Effect.gen(function* () {
      const h = yield* harness(
        nth =>
          nth === 0 ? [skeleton([runHeader(RUN)])] : [skeleton([runHeader(RUN), runMetric(2n, 9)])],
        nth =>
          nth === 0
            ? [{end: {status: RpcStatus.UNAVAILABLE, message: 'link lost'}}]
            : [{hang: true}],
      );
      h.mount(h.session.runDetail.watch(RUN));
      expect(yield* h.read(h.session.runDetail.state(RUN))).toMatchObject({
        _tag: 'Stale',
        why: 'disconnected',
        projection: {runId: RUN},
      });
      yield* h.elapse(1);
      const recovered = yield* h.read(h.session.runDetail.state(RUN));
      expect(recovered._tag).toBe('Live');
      if (recovered._tag === 'Live') {
        expect(recovered.projection.metrics.values[0]?.points[0]?.value).toBe(9);
      }
      expect(calls(h, SKELETON)).toHaveLength(2);
    }),
  );
});
