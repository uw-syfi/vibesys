import {test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  MachineStatsSchema,
  SubscribeRequestSchema,
  TailRequestSchema,
  TopicEntrySchema,
} from '@vibesys/proto';
import {machineStats, SUBSCRIBE_METHOD, type SubscriptionState} from '@vibesys/subscription';
import {GrpcError} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {GatewayCalls, GatewayConnection, Subscriptions, TAIL_METHOD} from '../src/index.js';
// test-isolation: Node HTTP/2 needs the gateway Fake alone; legacy contract exports load bun:test.
import {FakeGateway} from '../src/testing/fake-gateway.js';

const subscribeRequest = () =>
  toBinary(
    SubscribeRequestSchema,
    create(SubscribeRequestSchema, {
      topic: {case: 'machineStats', value: {}},
    }),
  );

const reading = (second: number) =>
  create(TopicEntrySchema, {
    key: 'machine',
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

test('a lost session ends a Subscribe stream and does not re-issue it, while a Tail still resumes', {
  timeout: 15000,
}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const connection = new GatewayConnection(fake, clock.scheduleTimeout);
  const abort = new AbortController();
  try {
    const subscribed = connection.stream(
      SUBSCRIBE_METHOD,
      subscribeRequest(),
      abort.signal,
      () => {},
    );
    const outcome = subscribed.catch((error: unknown) => error);
    const tail = connection.stream(
      TAIL_METHOD,
      toBinary(TailRequestSchema, create(TailRequestSchema, {runId: 'run-a', fromSeq: 1n})),
      abort.signal,
      () => {},
    );
    tail.catch(() => {});
    const open = await fake.nextSubscribe();
    await fake.nextTail();
    open.disconnect();
    const ended = await outcome;
    expect(ended).toBeInstanceOf(GrpcError);
    expect((ended as GrpcError).code).toBe(14);
    await clock.waitUntilScheduled(500);
    clock.advanceBy(500);
    // The Tail was issued again from the same loop that would have issued the subscription.
    await fake.nextTail();
    expect(fake.subscribed).toBe(1);
  } finally {
    abort.abort();
    connection.close();
    fake.close();
  }
});

/** Resolves once the subscription's state satisfies `wanted`, whatever order states arrive in. */
class Watched {
  readonly states: SubscriptionState<unknown>[] = [];
  #waiters: Array<() => void> = [];

  push(state: SubscriptionState<unknown>): void {
    this.states.push(state);
    for (const wake of this.#waiters.splice(0)) wake();
  }

  async until(wanted: (state: SubscriptionState<unknown>) => boolean): Promise<void> {
    while (!wanted(this.states.at(-1) ?? {kind: 'connecting', step: 'gateway', attempt: 0}))
      await new Promise<void>(resolve => this.#waiters.push(resolve));
  }
}

test('Subscriptions over a real session: live, stale when the connection drops, and live again with the new snapshot only', {
  timeout: 15000,
}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.scheduleTimeout);
  const hub = new Subscriptions(calls, clock.scheduleTimeout, () => clock.now());
  const watched = new Watched();
  const release = hub.observe('local', machineStats, state => watched.push(state));
  try {
    const first = await fake.nextSubscribe();
    first.send({case: 'entry', value: reading(1)});
    first.send({case: 'synced', value: {}});
    await watched.until(state => state.kind === 'live');
    first.disconnect();
    await watched.until(state => state.kind === 'stale');
    expect(watched.states.at(-1)).toMatchObject({kind: 'stale', reason: 'ended', attempt: 1});
    await clock.waitUntilScheduled(1000);
    clock.advanceBy(1000);
    const second = await fake.nextSubscribe();
    // Until the new snapshot is whole, the old value stays.
    second.send({case: 'entry', value: reading(2)});
    expect(watched.states.at(-1)?.kind).toBe('stale');
    second.send({case: 'synced', value: {}});
    await watched.until(state => state.kind === 'live');
    const live = watched.states.at(-1);
    expect(live?.kind === 'live' ? live.value : null).toMatchObject({
      sampledAt: {seconds: 2n},
    });
    expect(fake.subscribed).toBe(2);
  } finally {
    release();
    await Promise.race([fake.nextSubscribe().then(() => undefined), Promise.resolve()]);
    calls.close();
    fake.close();
  }
});

test('leaving the last observer ends the gateway stream', {timeout: 15000}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.scheduleTimeout);
  const hub = new Subscriptions(calls, clock.scheduleTimeout, () => clock.now());
  const release = hub.observe('local', machineStats, () => {});
  try {
    const open = await fake.nextSubscribe();
    open.send({case: 'entry', value: reading(1)});
    open.send({case: 'synced', value: {}});
    release();
    await open.closed;
    expect(clock.pending).toBe(0);
  } finally {
    calls.close();
    fake.close();
  }
});
