import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {AbsenceReason, SubscribeRequestSchema, TopicFailureCode} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Schema from 'effect/Schema';
import {machineView} from '../state/machine-stats.js';
import {
  HEARTBEAT_FRAME,
  type MachineFigures,
  RUNS_ENTRY_FRAME,
  refusedFrame,
  SUBSCRIBE,
  SYNCED_FRAME,
  servingMachineStats,
  statsFrame,
  steadyStream,
  TWO_GPUS,
} from '../testing/machine-stats.js';
import {
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {Step} from '../testing/transport/index.js';
import {RpcStatus} from './errors.js';

const scenario = generateScenario({seed: 11, subagents: 0, live: false});
const GIB = 1024 ** 3;

const harness = (streams: (nth: number) => ReadonlyArray<Step>) =>
  hostHarness(new FakeDaemon(scenario), {wrap: servingMachineStats(streams)});

const topicCalls = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === SUBSCRIBE);
const hellos = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('Hello'));

const view = (h: HostHarness) =>
  Effect.gen(function* () {
    const connection = yield* h.read(h.session.connection.state);
    return machineView(connection, yield* h.read(h.session.machine.model));
  });

/** The same stream for every call: a host whose topic works. */
const always = (steps: ReadonlyArray<Step>) => () => steps;

const NO_GPU_TOOL: MachineFigures = {
  cpuPercent: 3,
  memory: {absent: AbsenceReason.READ_FAILED},
  gpus: {absent: AbsenceReason.TOOL_NOT_FOUND},
};

describe('the machine stats feed', () => {
  it.effect('subscribes to the topic only while mounted, and shows the reading once synced', () =>
    Effect.gen(function* () {
      const h = yield* harness(always([statsFrame(TWO_GPUS), SYNCED_FRAME, {hang: true}]));
      yield* h.elapse(1);
      expect(topicCalls(h)).toEqual([]);
      const unmount = h.mount(h.session.machine.watch);
      yield* h.elapse(1);
      const [call] = topicCalls(h);
      expect(call && fromBinary(SubscribeRequestSchema, call.target.request).topic.case).toBe(
        'machineStats',
      );
      expect(yield* view(h)).toEqual({
        _tag: 'Shown',
        stale: null,
        sample: {
          cpuPercent: {_tag: 'Read', value: 37.5},
          memory: {_tag: 'Read', value: {usedBytes: 41 * GIB, totalBytes: 128 * GIB}},
          gpus: {
            _tag: 'Read',
            value: [
              {
                index: 0,
                name: 'NVIDIA H100 80GB HBM3',
                utilizationPercent: 92,
                memory: {usedBytes: 71 * GIB, totalBytes: 80 * GIB},
              },
              {
                index: 1,
                name: 'NVIDIA H100 80GB HBM3',
                utilizationPercent: 0,
                memory: {usedBytes: 0, totalBytes: 80 * GIB},
              },
            ],
          },
        },
      });
      unmount();
      yield* h.elapse(1);
      expect(topicCalls(h).map(one => one.open)).toEqual([false]);
      // A screen opened later starts from a new snapshot, not from what this one read.
      expect(yield* view(h)).toEqual({_tag: 'Waiting'});
    }),
  );

  it.effect.prop(
    'keeps the newest host reading when a screen leaves and rejoins before its stream ends',
    {
      before: Schema.Int.check(Schema.isBetween({minimum: 0, maximum: 49})),
      after: Schema.Int.check(Schema.isBetween({minimum: 50, maximum: 100})),
    },
    ({before, after}) =>
      Effect.gen(function* () {
        const endings: readonly Step[] = [
          refusedFrame(TopicFailureCode.UNSUPPORTED),
          {end: {status: RpcStatus.UNIMPLEMENTED}},
        ];
        for (const ending of endings) {
          const h = yield* harness(
            always([
              statsFrame({...TWO_GPUS, cpuPercent: before}),
              SYNCED_FRAME,
              {sleep: 1000},
              statsFrame({...TWO_GPUS, cpuPercent: after}),
              ending,
            ]),
          );
          const leave = h.mount(h.session.machine.watch);
          yield* h.elapse(0);
          expect(yield* view(h)).toMatchObject({sample: {cpuPercent: {value: before}}});

          // React can replace one reader with another before the shared atom's interruption runs.
          // The stream is still the source of truth: a value it accepts in between is not stale UI.
          leave();
          h.mount(h.session.machine.watch);
          yield* h.elapse(1);

          expect(topicCalls(h)).toHaveLength(1);
          expect(yield* view(h)).toEqual({
            _tag: 'Shown',
            sample: expect.objectContaining({cpuPercent: {_tag: 'Read', value: after}}),
            stale: 'unsupported',
          });
        }
      }),
    {arbitrary: {runs: 10}},
  );

  it.effect('says why a section has no reading, and keeps an empty GPU list apart from that', () =>
    Effect.gen(function* () {
      const none: MachineFigures = {...TWO_GPUS, gpus: []};
      const h = yield* harness(
        always([
          statsFrame(NO_GPU_TOOL),
          SYNCED_FRAME,
          {sleep: 1000},
          statsFrame(none),
          {hang: true},
        ]),
      );
      h.mount(h.session.machine.watch);
      yield* h.elapse(0);
      expect(yield* view(h)).toMatchObject({
        sample: {
          cpuPercent: {_tag: 'Read', value: 3},
          memory: {_tag: 'Absent', cause: 'read-failed'},
          gpus: {_tag: 'Absent', cause: 'no-tool'},
        },
      });
      yield* h.elapse(2);
      expect(yield* view(h)).toMatchObject({sample: {gpus: {_tag: 'Read', value: []}}});
    }),
  );

  it.effect('keeps a reading current while values come, and stale once none came for 30s', () =>
    Effect.gen(function* () {
      // The values stop after the snapshot; heartbeats keep the stream alive.
      const heartbeats = Array.from({length: 8}, () => [{sleep: 5000}, HEARTBEAT_FRAME]).flat();
      const h = yield* harness(
        always([
          statsFrame(TWO_GPUS),
          SYNCED_FRAME,
          ...heartbeats,
          statsFrame(TWO_GPUS),
          {hang: true},
        ]),
      );
      h.mount(h.session.machine.watch);
      yield* h.elapse(29);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
      yield* h.elapse(2);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: 'not-updating'});
      // A value after 40s makes it current again, on the same stream.
      yield* h.elapse(10);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
      expect(topicCalls(h)).toHaveLength(1);
    }),
  );

  it.effect('stays current on a stream that keeps the topic cadence', () =>
    Effect.gen(function* () {
      const h = yield* harness(always(steadyStream(() => TWO_GPUS, 30)));
      h.mount(h.session.machine.watch);
      for (let second = 0; second < 300; second += 10) {
        yield* h.elapse(10);
        expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
      }
      expect(topicCalls(h)).toHaveLength(1);
    }),
  );

  it.effect.prop(
    "never reads the host's sample time: a clock far off or behind changes nothing",
    {sampledAtMs: Schema.Int.check(Schema.isBetween({minimum: -6e13, maximum: 2.5e14}))},
    ({sampledAtMs}) =>
      Effect.gen(function* () {
        const steps: Step[] = [statsFrame(TWO_GPUS, {sampledAtMs}), SYNCED_FRAME];
        for (let beat = 0; beat < 8; beat += 1) steps.push({sleep: 5000}, HEARTBEAT_FRAME);
        const h = yield* harness(always([...steps, {hang: true}]));
        h.mount(h.session.machine.watch);
        yield* h.elapse(29);
        expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
        yield* h.elapse(2);
        expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: 'not-updating'});
      }),
    {arbitrary: {runs: 10}},
  );

  it.effect('gives up on a stream silent past its lease, and opens another after a backoff', () =>
    Effect.gen(function* () {
      const h = yield* harness(nth =>
        nth === 0
          ? [statsFrame(TWO_GPUS), SYNCED_FRAME, {hang: true}]
          : [statsFrame(TWO_GPUS), SYNCED_FRAME, ...steadyStream(() => TWO_GPUS, 10)],
      );
      h.mount(h.session.machine.watch);
      yield* h.elapse(29);
      expect(topicCalls(h)).toHaveLength(1);
      yield* h.elapse(1);
      expect(topicCalls(h)[0]?.open).toBe(false);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: 'silent'});
      // The first backoff is at most half a second.
      yield* h.elapse(1);
      expect(topicCalls(h)).toHaveLength(2);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
      expect(hellos(h)).toHaveLength(1);
    }),
  );

  it.effect(
    'gives up on a snapshot that does not come within 15s, however many heartbeats do',
    () =>
      Effect.gen(function* () {
        const beats = Array.from({length: 10}, () => [{sleep: 2000}, HEARTBEAT_FRAME]).flat();
        const h = yield* harness(nth =>
          nth === 0 ? [...beats, {hang: true}] : steadyStream(() => TWO_GPUS, 10),
        );
        h.mount(h.session.machine.watch);
        yield* h.elapse(14);
        expect(yield* view(h)).toEqual({_tag: 'Waiting'});
        yield* h.elapse(1);
        expect(yield* view(h)).toEqual({_tag: 'Missing', why: 'silent'});
        yield* h.elapse(1);
        expect(topicCalls(h)).toHaveLength(2);
        expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
      }),
  );

  it.effect(
    'opens a new stream after a failed sampler, and waits out a host that cannot sample',
    () =>
      Effect.gen(function* () {
        const failing = yield* harness(nth =>
          nth === 0
            ? [refusedFrame(TopicFailureCode.SOURCE_FAILED)]
            : steadyStream(() => TWO_GPUS, 10),
        );
        failing.mount(failing.session.machine.watch);
        yield* failing.elapse(0);
        expect(yield* view(failing)).toEqual({_tag: 'Missing', why: 'read-failed'});
        yield* failing.elapse(1);
        expect(yield* view(failing)).toMatchObject({_tag: 'Shown', stale: null});

        const unable = yield* harness(always([refusedFrame(TopicFailureCode.UNSUPPORTED)]));
        unable.mount(unable.session.machine.watch);
        yield* unable.elapse(120);
        expect(yield* view(unable)).toEqual({_tag: 'Missing', why: 'unsupported'});
        expect(topicCalls(unable)).toHaveLength(1);
      }),
  );

  it.effect('earns back its backoff from when a stream first synced, whatever comes after', () =>
    Effect.gen(function* () {
      // Four samplers fail at once, so a fifth failure in a row would wait 4s to 8s. The fifth
      // stream holds for a minute, then sends a second `Synced` (which the fold ignores) and fails.
      const held = steadyStream(() => TWO_GPUS, 6).slice(0, -1);
      const h = yield* harness(nth => {
        const failure = refusedFrame(TopicFailureCode.SOURCE_FAILED);
        if (nth < 4) return [failure];
        return nth === 4 ? [...held, SYNCED_FRAME, failure] : steadyStream(() => TWO_GPUS, 10);
      });
      h.mount(h.session.machine.watch);
      for (let step = 0; step < 400 && topicCalls(h)[4]?.open !== false; step += 1) {
        yield* h.elapse(0.25, 0.25);
      }
      expect(topicCalls(h)[4]?.open).toBe(false);
      // A stream that held for a minute starts the count over: the next waits at most 0.5s.
      yield* h.elapse(1);
      expect(topicCalls(h)).toHaveLength(6);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
    }),
  );

  it.effect('shows a host built before the topic as unsupported, and leaves its link up', () =>
    Effect.gen(function* () {
      // No wrap: the daemon's recording answers the topic as a method it does not know.
      const h = yield* hostHarness(new FakeDaemon(scenario));
      h.mount(h.session.machine.watch);
      yield* h.elapse(120);
      expect(yield* view(h)).toEqual({_tag: 'Missing', why: 'unsupported'});
      expect(topicCalls(h)).toHaveLength(1);
      expect(hellos(h)).toHaveLength(1);
      expect(yield* h.read(h.session.connection.state)).toMatchObject({_tag: 'Ready'});
    }),
  );

  it.effect(
    "holds a frame that breaks the topic's contract invalid, and keeps the reading it had",
    () =>
      Effect.gen(function* () {
        const broken: ReadonlyArray<ReadonlyArray<Step>> = [
          // A value of another topic.
          [RUNS_ENTRY_FRAME],
          // A second key, where the topic has one.
          [statsFrame(TWO_GPUS, {key: 'other'})],
          // A figure the schema rejects: more memory used than there is.
          [statsFrame({...TWO_GPUS, memory: {usedBytes: 2n, totalBytes: 1n}})],
        ];
        for (const frames of broken) {
          const h = yield* harness(
            always([statsFrame(TWO_GPUS), SYNCED_FRAME, ...frames, {hang: true}]),
          );
          h.mount(h.session.machine.watch);
          yield* h.elapse(120);
          expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: 'bad-readings'});
          // Only a new link may bring a host that keeps the contract.
          expect(topicCalls(h)).toHaveLength(1);
        }
      }),
  );

  it.effect('hands a lost link to the connection, and reads again on the new link', () =>
    Effect.gen(function* () {
      const h = yield* harness(nth =>
        nth === 0
          ? [
              statsFrame(TWO_GPUS),
              SYNCED_FRAME,
              {sleep: 5000},
              {end: {status: RpcStatus.UNAVAILABLE}},
            ]
          : steadyStream(() => TWO_GPUS, 10),
      );
      h.mount(h.session.machine.watch);
      yield* h.elapse(5);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: 'disconnected'});
      yield* h.elapse(5);
      expect(hellos(h)).toHaveLength(2);
      expect(topicCalls(h)).toHaveLength(2);
      expect(yield* view(h)).toMatchObject({_tag: 'Shown', stale: null});
    }),
  );
});
