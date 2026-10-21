import {expect, test} from 'bun:test';
import {create, fromBinary} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {RecordSchema, TailRequestSchema} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {FakeClock} from '@vibesys/transport/testing';
import type {SnapshotFrom} from 'xstate';
import {gatewayRunMachine} from '../../state/index.js';
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
      snapshot.context.load?.complete === true && Object.keys(snapshot.context.text).length === 1,
  );
  const tail = transport.calls.find(call => call.method === `${SERVICE}Tail`);
  if (!tail) throw new Error('Tail required');
  expect(fromBinary(TailRequestSchema, tail.request).fromSeq).toBe(4n);
  expect(transport.calls.filter(call => call.method === `${SERVICE}Skeleton`)).toHaveLength(1);
  expect(transport.calls.filter(call => call.method === `${SERVICE}GetBlobs`)).toHaveLength(1);
  const calls = transport.calls.length;
  actor.send({type: 'ui', action: {type: 'view', view: {at: 3}}});
  expect(actor.getSnapshot().context.ui.view.at).toBe(3);
  expect(transport.calls).toHaveLength(calls);
  actor.send({type: 'ui', action: {type: 'select-stream', streamId: 'build-log'}});
  await reached(actor, snapshot => snapshot.context.ui.views['build-log']?.lines.length === 1);
  expect(transport.activeStreams).toBe(2);
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
  await reached(reopened, snapshot => Object.keys(snapshot.context.text).length === 1);
  expect(transport.calls.filter(call => call.method.endsWith('GetBlobs'))).toHaveLength(1);
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
