import {expect, test} from 'bun:test';
import {create, fromBinary} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {
  RecordSchema,
  RunLiveness,
  TailRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {FakeClock} from '@vibesys/transport/testing';
import type {SnapshotFrom} from 'xstate';
import {
  gatewayRunMachine,
  headerView,
  projectSkeleton,
  runHasEnded,
  scrubberView,
  skeletonScreenState,
} from '../../state/index.js';
import {startGatewayRun} from '../index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
const SERVICE = '/vibesys.api.v1.HostService/';
function reached(
  actor: ReturnType<typeof startGatewayRun>,
  predicate: (snapshot: SnapshotFrom<typeof gatewayRunMachine>) => boolean,
): Promise<void> {
  if (predicate(actor.getSnapshot())) return Promise.resolve();
  return new Promise(resolve => {
    const subscription = actor.subscribe(snapshot => {
      if (predicate(snapshot)) {
        subscription.unsubscribe();
        resolve();
      }
    });
  });
}

test('model: every setting, content and log operation belongs to its active state', () => {
  const fixture = storeRunFixture();
  const skeleton = foldSkeleton(seedSkeleton(), fixture.records);
  checkMachine({
    machine: gatewayRunMachine,
    input: {seed: SEED},
    event: (random, live) =>
      random.pick([
        {type: 'toggle', enabled: random.pick([true, false])},
        {
          type: 'loaded',
          load: {
            skeleton,
            committedSeq: skeleton.throughSeq ?? 0n,
            nextSeq: (skeleton.throughSeq ?? 0n) + 1n,
            complete: true,
          },
          failed: false,
        },
        {type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}},
        ...live.operations.flatMap(id => [
          live.error(id, new Error('Fault')),
          ...(id.endsWith('setting')
            ? [live.done(id, true), live.done(id, false)]
            : id === 'content'
              ? [live.done(id, {hash: 'hash', text: 'Content'})]
              : []),
        ]),
        ...live.delays.map(id => live.fire(id)),
      ]),
  });
});

test('Skeleton then Tail, local seek, verified cached content, logs and cancellation', async () => {
  const fixture = storeRunFixture('blobs');
  const transport = new FakeRunTransport({...fixture, initialCount: 3});
  const cache = new FakeSkeletonCache();
  const clock = new FakeClock();
  let enabled = true;
  const ports = {
    reader: gatewayReader(transport, cache, clock.schedule),
    setting: async (value: boolean | undefined) => {
      enabled = value ?? enabled;
      return enabled;
    },
  };
  const actor = startGatewayRun(ports, clock.schedule, {seed: SEED});
  await reached(
    actor,
    snapshot =>
      snapshot.context.load?.complete === true && Object.keys(snapshot.context.text).length === 2,
  );
  const tail = transport.calls.find(call => call.method === `${SERVICE}Tail`);
  if (!tail) throw new Error('Tail required');
  expect(fromBinary(TailRequestSchema, tail.request).fromSeq).toBe(4n);
  expect(transport.calls.filter(call => call.method === `${SERVICE}Skeleton`)).toHaveLength(1);
  expect(transport.calls.filter(call => call.method === `${SERVICE}GetBlobs`)).toHaveLength(2);
  expect(Object.values(actor.getSnapshot().context.text).sort()).toEqual([
    'Steady throughput',
    'Throughput',
  ]);
  const calls = transport.calls.length;
  actor.send({type: 'ui', action: {type: 'view', view: {at: 3}}});
  expect(actor.getSnapshot().context.ui.view.at).toBe(3);
  expect(transport.calls).toHaveLength(calls);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await reached(actor, snapshot => snapshot.context.ui.views['build-log']?.lines.length === 1);
  expect(transport.activeStreams).toBe(3);
  actor.send({type: 'toggle', enabled: false});
  await reached(actor, snapshot => snapshot.matches('off'));
  await Promise.resolve();
  expect(
    transport.calls.every(
      call =>
        call.signal.aborted || call.method.endsWith('Skeleton') || call.method.endsWith('GetBlobs'),
    ),
  ).toBe(true);
  actor.stop();
  const reopened = startGatewayRun({...ports, setting: async () => true}, clock.schedule, {
    seed: SEED,
  });
  await reached(reopened, snapshot => Object.keys(snapshot.context.text).length === 2);
  expect(transport.calls.filter(call => call.method.endsWith('GetBlobs'))).toHaveLength(2);
  reopened.stop();
});

test('off is inert, live records append while scrubbed and switching streams cancels the prior follower', async () => {
  const fixture = storeRunFixture('live');
  const transport = new FakeRunTransport(fixture);
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
      setting: async value => value ?? false,
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(actor, snapshot => snapshot.matches('off'));
  expect(transport.calls).toHaveLength(0);
  actor.send({type: 'toggle', enabled: true});
  await reached(actor, snapshot => snapshot.context.load?.complete === true);
  actor.send({type: 'ui', action: {type: 'view', view: {at: 3}}});
  const seq = (fixture.records.at(-1)?.seq ?? 0n) + 1n;
  transport.publish([
    create(RecordSchema, {
      seq,
      body: {case: 'lifecycle', value: {state: {case: 'parked', value: {}}}},
    }),
  ]);
  await reached(actor, snapshot => snapshot.context.load?.skeleton.throughSeq === seq);
  expect(actor.getSnapshot().context.ui.view.at).toBe(3);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await reached(actor, snapshot => snapshot.context.ui.views['build-log'] !== undefined);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'missing'}});
  await reached(actor, snapshot => snapshot.context.error === 'Log unavailable');
  actor.stop();
});

test('failed unary and corrupt content surface errors without using the legacy path', async () => {
  const fixture = storeRunFixture();
  for (const operation of ['Skeleton', 'GetBlobs']) {
    const clock = new FakeClock();
    const transport = new FakeRunTransport(fixture);
    transport.failures.set(`${SERVICE}${operation}`, new Error('Fault'));
    const actor = startGatewayRun(
      {
        reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
        setting: async () => true,
      },
      new FakeClock().schedule,
      {seed: SEED},
    );
    await reached(actor, snapshot => snapshot.context.error !== null);
    expect(actor.getSnapshot().context.error).toBe(
      operation === 'Skeleton' ? 'Disconnected' : 'Content unavailable',
    );
    actor.stop();
  }
});

test('setting, content and first-log deadlines use the injected clock', async () => {
  const fixture = storeRunFixture();
  const clock = new FakeClock();
  const transport = new FakeRunTransport(fixture);
  const reader = gatewayReader(transport, new FakeSkeletonCache(), clock.schedule);
  let finishSetting: (enabled: boolean) => void = () => {
    throw new Error('setting not started');
  };
  const checking = startGatewayRun(
    {
      reader,
      setting: () =>
        new Promise(resolve => {
          finishSetting = resolve;
        }),
    },
    clock.schedule,
    {seed: SEED},
  );
  clock.advanceBy(15000);
  expect(checking.getSnapshot().matches('failed')).toBe(true);
  finishSetting(true);
  await Promise.resolve();
  expect(transport.calls).toHaveLength(0);
  checking.stop();
  transport.held.add(`${SERVICE}GetBlobs`);
  transport.held.add(`${SERVICE}TailStream`);
  const actor = startGatewayRun({reader, setting: async () => true}, clock.schedule, {seed: SEED});
  await reached(actor, snapshot => snapshot.matches({active: {content: 'reading'}}));
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await transport.called(`${SERVICE}GetBlobs`);
  await transport.called(`${SERVICE}TailStream`);
  clock.advanceBy(30000);
  expect(actor.getSnapshot().context.error).toBe('Log timeout');
  expect(transport.calls.find(call => call.method.endsWith('GetBlobs'))?.signal.aborted).toBe(true);
  expect(transport.calls.find(call => call.method.endsWith('TailStream'))?.signal.aborted).toBe(
    true,
  );
  actor.stop();
});

test('each objective hash is attempted once even when content fails', async () => {
  const fixture = storeRunFixture('blobs');
  const clock = new FakeClock();
  const transport = new FakeRunTransport(fixture);
  transport.failures.set(`${SERVICE}GetBlobs`, new Error('Fault'));
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
      setting: async () => true,
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(
    actor,
    snapshot =>
      transport.calls.filter(call => call.method.endsWith('GetBlobs')).length === 2 &&
      snapshot.matches({active: {content: 'idle'}}),
  );
  expect(transport.calls.filter(call => call.method.endsWith('GetBlobs'))).toHaveLength(2);
  expect(actor.getSnapshot().context.error).toBe('Content unavailable');
  actor.stop();
});

test.each([
  'running',
  'paused',
] as const)('lock loss interrupts a %s run without changing history', async phase => {
  const fixture = storeRunFixture('live');
  const seq = (fixture.records.at(-1)?.seq ?? 0n) + 1n;
  const transport = new FakeRunTransport({
    ...fixture,
    records: [
      ...fixture.records,
      create(RecordSchema, {
        seq,
        epoch: 1,
        body: {
          case: 'lifecycle',
          value: {
            state: {
              case: 'running',
              value: phase === 'paused' ? {hold: {reason: {case: 'operator', value: {}}}} : {},
            },
          },
        },
      }),
    ],
  });
  const clock = new FakeClock();
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
      setting: async () => true,
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(actor, snapshot => snapshot.context.load?.complete === true);
  await transport.called(`${SERVICE}WatchRuns`);
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'other',
      liveness: RunLiveness.INTERRUPTED,
      head: {process: {epoch: 1}},
    }),
  );
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'run-a',
      liveness: RunLiveness.ALIVE,
      head: {process: {epoch: 1}},
    }),
  );
  await reached(actor, snapshot => snapshot.context.hint?.value === RunLiveness.ALIVE);
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'run-a',
      liveness: RunLiveness.INTERRUPTED,
      head: {process: {epoch: 1}},
    }),
  );
  await reached(actor, snapshot => snapshot.context.hint?.value === RunLiveness.INTERRUPTED);
  const {load, hint, ui} = actor.getSnapshot().context;
  if (!load) throw new Error('loaded run required');
  const whole = projectSkeleton(load.skeleton, seq);
  const screen = skeletonScreenState(SEED, load.skeleton, whole, ui, hint);
  expect(headerView(screen).status.label).toBe('Interrupted');
  expect(runHasEnded(screen)).toBe(true);
  expect(scrubberView(screen.run.trace, screen.view, runHasEnded(screen))?.latestLabel).toBe('End');
  expect(load.skeleton.lifecycles.at(-1)?.lifecycle?.state.case).toBe('running');
  expect(headerView(skeletonScreenState(SEED, load.skeleton, whole, ui)).status.label).toBe(
    phase === 'paused' ? 'Paused by you' : 'Running',
  );
  expect(actor.getSnapshot().context.error).toBeNull();
  actor.stop();
  await Promise.resolve();
  expect(
    transport.calls
      .filter(call => call.method.endsWith('WatchRuns'))
      .every(call => call.signal.aborted),
  ).toBe(true);
});

test.each([
  'short',
  'live',
] as const)('Tail loss on %s distinguishes final history from a live connection', async kind => {
  for (const failure of ['closed', 'error'] as const) {
    const fixture = storeRunFixture(kind);
    const transport = new FakeRunTransport(fixture);
    const clock = new FakeClock();
    const actor = startGatewayRun(
      {
        reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
        setting: async () => true,
      },
      clock.schedule,
      {seed: SEED},
    );
    await reached(actor, snapshot => snapshot.context.load?.complete === true);
    await reached(
      actor,
      snapshot =>
        snapshot.matches({active: {content: 'idle'}}) &&
        Object.keys(snapshot.context.text).length > 0,
    );
    expect(clock.pending).toBe(0);
    const through = actor.getSnapshot().context.load?.skeleton.throughSeq;
    if (failure === 'closed') transport.close(`${SERVICE}Tail`);
    else transport.fail(`${SERVICE}Tail`, new Error('connection lost'));
    // The retry timer is armed only after the stream's outcome has been consumed.
    await reached(actor, () => clock.pending > 0);
    expect(actor.getSnapshot().context.error).toBe(kind === 'short' ? null : 'Disconnected');
    expect(actor.getSnapshot().context.load?.skeleton.throughSeq).toBe(through);
    if (kind === 'live') {
      transport.recover(`${SERVICE}Tail`);
      clock.advanceBy(1000);
      await reached(actor, snapshot => snapshot.context.error === null);
    }
    actor.stop();
    expect(clock.pending).toBe(0);
  }
});

test.each([
  'deadline',
  'closed',
  'error',
] as const)('liveness %s recovers independently and cancels with the reader', async failure => {
  const fixture = storeRunFixture('live');
  const clock = new FakeClock();
  const transport = new FakeRunTransport(fixture);
  if (failure === 'deadline') transport.held.add(`${SERVICE}WatchRuns`);
  const actor = startGatewayRun(
    {
      reader: gatewayReader(transport, new FakeSkeletonCache(), clock.schedule),
      setting: async () => true,
    },
    clock.schedule,
    {seed: SEED},
  );
  await reached(
    actor,
    snapshot =>
      snapshot.context.load?.complete === true && Object.keys(snapshot.context.text).length > 0,
  );
  await transport.called(`${SERVICE}WatchRuns`);
  if (failure === 'deadline') clock.advanceBy(30000);
  else if (failure === 'closed') transport.close(`${SERVICE}WatchRuns`);
  else transport.fail(`${SERVICE}WatchRuns`, new Error('offline'));
  await reached(actor, snapshot => snapshot.context.error === 'Disconnected');
  transport.release(`${SERVICE}WatchRuns`);
  transport.recover(`${SERVICE}WatchRuns`);
  clock.advanceBy(1000);
  await reached(actor, snapshot => snapshot.context.error === null);
  transport.observe(
    create(WatchRunsResponseSchema, {
      runId: 'run-a',
      liveness: RunLiveness.INTERRUPTED,
      head: {process: {epoch: 1}},
    }),
  );
  await reached(actor, snapshot => snapshot.context.hint?.value === RunLiveness.INTERRUPTED);
  transport.observe(create(WatchRunsResponseSchema, {runId: 'run-a', removed: true}));
  await reached(actor, snapshot => snapshot.context.hint === undefined);
  actor.stop();
  expect(clock.pending).toBe(0);
  expect(transport.calls.filter(call => call.method.endsWith('WatchRuns'))).toHaveLength(2);
});
