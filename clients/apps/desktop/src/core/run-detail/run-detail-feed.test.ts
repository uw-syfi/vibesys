import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {HostService, SkeletonRequestSchema, TailRequestSchema} from '@vibesys/proto';
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import type * as Atom from 'effect/reactivity/Atom';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Scheduler from 'effect/Scheduler';
import * as TestClock from 'effect/testing/TestClock';
import {daemonRecording, FakeDaemon, generateScenario} from '../../testing/orchestrator/index.js';
import {
  rawSkeleton,
  runHeader,
  runMetric,
  SKELETON,
  servingRunDetail,
  skeleton,
  TAIL,
  tail,
} from '../../testing/run-detail/index.js';
import {makeQuiescence} from '../../testing/settle.js';
import {makeFakeTransport, type Step} from '../../testing/transport/index.js';
import {makeClient} from '../client/make-client.js';
import {makeHostConnection} from '../connection.js';
import {RpcStatus} from '../errors.js';
import {makeOrchestratorClient} from '../orchestrator-client.js';
import {makeRunDetailFeed} from './index.js';

const RUN = 'run-dynamic';
const scenario = generateScenario({seed: 23, subagents: 0, live: false});

const harness = (
  skeletons: (nth: number) => ReadonlyArray<Step>,
  tails: (nth: number) => ReadonlyArray<Step>,
) =>
  Effect.gen(function* () {
    const {scheduler, settle} = yield* makeQuiescence;
    const recording = servingRunDetail(skeletons, tails)(daemonRecording(new FakeDaemon(scenario)));
    const fake = makeFakeTransport(recording);
    const made = yield* Effect.gen(function* () {
      const client = yield* makeClient(HostService, 'local');
      const connection = yield* makeHostConnection(yield* makeOrchestratorClient('local'));
      const ambient = yield* Effect.context<never>();
      const feed = yield* makeRunDetailFeed({client, connection, ambient});
      return {feed, connection};
    }).pipe(Effect.provide(fake.layer), Effect.provideService(Scheduler.Scheduler, scheduler));
    const registry = AtomRegistry.make();
    return {
      ...made,
      fake,
      mount: <A>(atom: Atom.Atom<A>): (() => void) => registry.mount(atom),
      read: <A>(atom: Atom.Atom<A>) =>
        Effect.gen(function* () {
          yield* settle;
          return registry.get(atom);
        }),
      elapse: (seconds: number) =>
        Effect.gen(function* () {
          yield* settle;
          yield* TestClock.adjust(Duration.seconds(seconds));
          yield* settle;
        }),
    };
  });

type Harness = Effect.Success<ReturnType<typeof harness>>;

const calls = (h: Harness, method: string) =>
  h.fake.calls().filter(call => call.target.method === method);

describe('the generic run detail feed', () => {
  it.effect('acquires Skeleton, tails from next_seq, and releases the Tail on interruption', () =>
    Effect.gen(function* () {
      const h = yield* harness(
        () => [skeleton([runHeader(RUN)], 2n)],
        () => [tail([runMetric(2n, 12)]), {hang: true}],
      );
      expect(yield* h.read(h.feed.state(RUN))).toEqual({_tag: 'Loading'});
      const unmount = h.mount(h.feed.watch(RUN));
      const state = yield* h.read(h.feed.state(RUN));
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
      expect(yield* h.read(h.feed.state(RUN))).toEqual({_tag: 'Loading'});
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
      h.mount(h.feed.watch(RUN));
      const state = yield* h.read(h.feed.state(RUN));
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
      recovered.mount(recovered.feed.watch(RUN));
      expect(yield* recovered.read(recovered.feed.state(RUN))).toMatchObject({_tag: 'Live'});
      expect(calls(recovered, SKELETON)).toHaveLength(2);

      const broken = yield* harness(
        () => [invalid],
        () => [{hang: true}],
      );
      broken.mount(broken.feed.watch(RUN));
      expect(yield* broken.read(broken.feed.state(RUN))).toMatchObject({
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
      h.mount(h.feed.watch(RUN));
      const refused = yield* h.read(h.feed.state(RUN));
      expect(refused).toMatchObject({
        _tag: 'Refused',
        fault: {kind: 'Unimplemented', retryable: false},
        projection: {runId: RUN},
      });
      expect(calls(h, SKELETON)).toHaveLength(1);
      yield* h.feed.retry(RUN);
      expect(yield* h.read(h.feed.state(RUN))).toMatchObject({_tag: 'Live'});
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
      h.mount(h.feed.watch(RUN));
      expect(yield* h.read(h.feed.state(RUN))).toMatchObject({
        _tag: 'Stale',
        why: 'disconnected',
        projection: {runId: RUN},
      });
      yield* h.elapse(1);
      const recovered = yield* h.read(h.feed.state(RUN));
      expect(recovered._tag).toBe('Live');
      if (recovered._tag === 'Live') {
        expect(recovered.projection.metrics.values[0]?.points[0]?.value).toBe(9);
      }
      expect(calls(h, SKELETON)).toHaveLength(2);
    }),
  );
});
