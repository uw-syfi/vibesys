import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  MachineStatsSchema,
  type SubscribeRequest,
  SubscribeRequestSchema,
  subscribeTiming,
  TopicEntrySchema,
} from '@vibesys/proto';
import {type SubscriptionState, type SubscriptionUpdate, TOPICS} from '@vibesys/subscription';
import {hubRig, settle} from '../testing/hub-rig.js';

const A = 'ssh:host-a';
const B = 'ssh:host-b';
const topic = TOPICS.machineStats;

function reading(second: number, key = 'machine') {
  return create(TopicEntrySchema, {
    key,
    value: {
      case: 'machineStats',
      value: create(MachineStatsSchema, {
        sampledAt: timestampFromMs(second * 1000),
        cpu: {case: 'cpuUnavailable', value: {reason: 1}},
        memory: {case: 'memoryUnavailable', value: {reason: 1}},
        gpu: {case: 'gpuUnavailable', value: {reason: 1}},
      }),
    },
  });
}

/** A request that differs from the plain one by a field this build does not know: a stand-in for a parameter. */
function parameterized(number: number): SubscribeRequest {
  const request = create(SubscribeRequestSchema, {topic: {case: 'machineStats', value: {}}});
  request.$unknown = [{no: 1000 + number, wireType: 0, data: new Uint8Array([number])}];
  return request;
}

test('an observer that throws on its first delivery leaves no stream and no timer behind', () => {
  const {gateway, clock, subscriptions} = hubRig();
  expect(() =>
    subscriptions.observe(A, topic, () => {
      throw new Error('screen bug');
    }),
  ).toThrow('screen bug');
  expect(gateway.streams).toHaveLength(1);
  expect(gateway.open).toHaveLength(0);
  expect(clock.pending).toBe(0);
  // The next observer starts a machine of its own rather than joining a dead one.
  subscriptions.observe(A, topic, () => {});
  expect(gateway.open).toHaveLength(1);
});

test('a relay whose receiver throws on its first delivery is reported, still owned, and released by its handle', () => {
  const {gateway, clock, subscriptions, reported} = hubRig();
  const handle = subscriptions.relay(
    A,
    topic.request,
    undefined,
    () => {
      throw new Error('port bug');
    },
    () => {},
  );
  expect(reported.length).toBeGreaterThan(0);
  expect(gateway.open).toHaveLength(1);
  handle.cancel();
  expect(gateway.open).toHaveLength(0);
  expect(clock.pending).toBe(0);
});

test('two instances of one topic with different requests open two different requests', () => {
  const {gateway, subscriptions} = hubRig();
  const heard: SubscriptionUpdate[][] = [[], []];
  for (const [index, number] of [1, 2].entries())
    subscriptions.relay(
      A,
      parameterized(number),
      undefined,
      update => heard[index]?.push(update),
      () => {},
    );
  // The same request again joins the first.
  subscriptions.relay(
    A,
    parameterized(1),
    undefined,
    () => {},
    () => {},
  );
  expect(gateway.streams).toHaveLength(2);
  expect(gateway.streams.map(stream => stream.request.$unknown?.[0]?.no)).toEqual([1001, 1002]);
});

test("host removal, retry and the keys of one host leave another host's stream alone", async () => {
  const {gateway, subscriptions} = hubRig();
  const states: Record<string, SubscriptionState<unknown>> = {};
  for (const host of [A, B])
    subscriptions.observe(host, topic, state => {
      states[host] = state;
    });
  expect(gateway.streams.map(stream => stream.host)).toEqual([A, B]);
  const [first, second] = gateway.streams;

  subscriptions.closeHost(A);
  expect(states[A]).toEqual({kind: 'failed', reason: 'closed', retrying: false});
  expect(first?.open).toBe(false);
  expect(second?.open).toBe(true);
  expect(states[B]?.kind).toBe('connecting');

  // Both lose their streams; retrying one reopens only it.
  const again = hubRig();
  for (const host of [A, B]) again.subscriptions.observe(host, topic, () => {});
  for (const stream of again.gateway.streams) stream.fail();
  await settle();
  expect(again.gateway.open).toHaveLength(0);
  again.subscriptions.retry(B);
  expect(again.gateway.open.map(stream => stream.host)).toEqual([B]);
});

test('wake reaches every host: a live stream with no frame within a beat is stale on each', async () => {
  const {gateway, subscriptions, clock} = hubRig();
  const states: Record<string, SubscriptionState<unknown>> = {};
  for (const host of [A, B])
    subscriptions.observe(host, topic, state => {
      states[host] = state;
    });
  for (const stream of gateway.streams) {
    stream.reached('runs');
    stream.send({case: 'entry', value: reading(1)});
    stream.send({case: 'synced', value: {}});
  }
  expect([states[A]?.kind, states[B]?.kind]).toEqual(['live', 'live']);
  subscriptions.wake();
  clock.advanceBy(subscribeTiming.heartbeatMs + 2_000);
  await settle();
  expect([states[A]?.kind, states[B]?.kind]).toEqual(['stale', 'stale']);
  // Without a wake the same silence is still within the lease.
  const quiet = hubRig();
  quiet.subscriptions.observe(A, topic, state => {
    states[A] = state;
  });
  quiet.gateway.streams[0]?.reached('runs');
  quiet.gateway.streams[0]?.send({case: 'entry', value: reading(1)});
  quiet.gateway.streams[0]?.send({case: 'synced', value: {}});
  quiet.clock.advanceBy(subscribeTiming.heartbeatMs + 2_000);
  expect(states[A]?.kind).toBe('live');
});

test("an observer of one key hears that key's entries, and the stream's own updates", () => {
  const {gateway, subscriptions} = hubRig();
  const heard: Record<string, string[]> = {machine: [], other: []};
  for (const key of ['machine', 'other'])
    subscriptions.relay(
      A,
      topic.request,
      key,
      update => heard[key]?.push(update.kind),
      () => {},
    );
  gateway.streams[0]?.reached('runs');
  gateway.streams[0]?.send({case: 'entry', value: reading(1)});
  gateway.streams[0]?.send({case: 'synced', value: {}});
  gateway.streams[0]?.send({case: 'removed', value: 'machine'});
  expect(heard['machine']).toEqual([
    'begin',
    'control',
    'control',
    'entry',
    'synced',
    'control',
    'removed',
  ]);
  expect(heard['other']).toEqual(['begin', 'control', 'control', 'synced', 'control']);
});

test('a frame that reaches the hub after its stream was left changes nothing', async () => {
  const {gateway, subscriptions, clock} = hubRig();
  let state: SubscriptionState<unknown> | undefined;
  subscriptions.observe(A, topic, each => {
    state = each;
  });
  const [old] = gateway.streams;
  old?.reached('runs');
  old?.send({case: 'entry', value: reading(1)});
  old?.send({case: 'synced', value: {}});
  // The lease runs out, the stream is left, and the next one opens.
  clock.advanceBy(subscribeTiming.leaseMs);
  clock.advanceBy(1_000);
  await settle();
  const next = gateway.streams.at(-1);
  expect(next).not.toBe(old);
  next?.reached('runs');
  // A key the topic cannot hold, from the stream that is gone, must not reach the machine.
  old?.sendLate({case: 'entry', value: reading(9, 'ghost')});
  old?.sendLate({case: 'synced', value: {}});
  next?.send({case: 'entry', value: reading(2)});
  next?.send({case: 'synced', value: {}});
  expect(state?.kind).toBe('live');
  expect(state?.kind === 'live' ? state.value : null).toMatchObject({sampledAt: {seconds: 2n}});
});

test('after a host is closed the next observer gets a fresh machine and stream', () => {
  const {gateway, subscriptions} = hubRig();
  subscriptions.observe(A, topic, () => {});
  subscriptions.closeHost(A);
  expect(gateway.open).toHaveLength(0);
  let state: SubscriptionState<unknown> | undefined;
  subscriptions.observe(A, topic, each => {
    state = each;
  });
  expect(gateway.streams).toHaveLength(2);
  expect(gateway.open).toHaveLength(1);
  expect(state?.kind).toBe('connecting');
});

test('an observer cancelled by another in the same update does not hear it', () => {
  const {gateway, subscriptions} = hubRig();
  const heard: string[] = [];
  const later: {cancel(): void}[] = [];
  subscriptions.relay(
    A,
    topic.request,
    undefined,
    update => {
      if (update.kind === 'entry') later[0]?.cancel();
    },
    () => {},
  );
  later.push(
    subscriptions.relay(
      A,
      topic.request,
      undefined,
      update => heard.push(update.kind),
      () => {},
    ),
  );
  gateway.streams[0]?.reached('runs');
  gateway.streams[0]?.send({case: 'entry', value: reading(1)});
  expect(heard).not.toContain('entry');
});
