import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {SubscribeRequestSchema, TopicFailureCode} from '@vibesys/proto';
import {type SubscriptionState, type SubscriptionUpdate, TOPICS} from '@vibesys/subscription';
import {FakeClock} from '@vibesys/transport/testing';
import {FakeSubscribeStreams} from '../testing/fake-subscribe-streams.js';
import {SubscriptionFailed, Subscriptions} from './subscriptions.js';

const HOST = 'ssh:host-a';
const topic = TOPICS.machineStats;

function hub() {
  const clock = new FakeClock();
  const gateway = new FakeSubscribeStreams();
  const reported: unknown[] = [];
  const subscriptions = new Subscriptions(
    gateway,
    clock.schedule,
    () => clock.now(),
    error => reported.push(error),
  );
  return {clock, gateway, subscriptions, reported};
}

async function settle(): Promise<void> {
  for (let drain = 0; drain < 5; drain += 1) await Promise.resolve();
}

test('an observer that throws is reported and does not stop the others or the machine', () => {
  const {gateway, subscriptions, reported} = hub();
  const heard: SubscriptionState<unknown>[] = [];
  let broken = false;
  subscriptions.watch(HOST, topic, () => {
    if (broken) throw new Error('screen bug');
  });
  broken = true;
  subscriptions.watch(HOST, topic, state => heard.push(state));
  gateway.streams[0]?.reached('runs');
  expect(heard.map(state => state.kind)).toEqual(['connecting', 'connecting']);
  expect(reported.length).toBeGreaterThan(0);
  gateway.streams[0]?.send({case: 'heartbeat', value: {}});
  expect(gateway.open).toHaveLength(1);
});

test("a relay receives the machine's updates, restricted to its key, then ends when the host closes", () => {
  const {gateway, subscriptions} = hub();
  const heard: SubscriptionUpdate[] = [];
  const ends: (string | undefined)[] = [];
  subscriptions.observe(
    HOST,
    topic.request,
    'machine',
    update => heard.push(update),
    error => ends.push(error),
  );
  expect(heard.map(update => update.kind)).toEqual(['begin', 'control']);
  subscriptions.closeHost(HOST);
  expect(heard.at(-1)).toEqual({
    kind: 'control',
    control: {kind: 'failed', reason: 'closed', retrying: false},
  });
  expect(ends).toEqual([undefined]);
  expect(gateway.open).toHaveLength(0);
});

test('first resolves with the live value, and rejects with the reason a host will not give one', async () => {
  const live = hub();
  const wanted = new AbortController();
  const value = live.subscriptions.first(HOST, topic, wanted.signal);
  live.gateway.streams[0]?.reached('runs');
  expect(live.gateway.open).toHaveLength(1);
  void value.catch(() => {});

  const refused = hub();
  const never = refused.subscriptions.first(HOST, topic, new AbortController().signal);
  refused.gateway.streams[0]?.reached('runs');
  refused.gateway.streams[0]?.send({
    case: 'failed',
    value: {code: TopicFailureCode.UNSUPPORTED, detail: ''},
  });
  await expect(never).rejects.toEqual(new SubscriptionFailed('unsupported', false));
  await settle();
  expect(refused.gateway.open).toHaveLength(0);
  expect(refused.clock.pending).toBe(0);

  wanted.abort(new Error('screen left'));
  await expect(value).rejects.toThrow('screen left');
  await settle();
  expect(live.gateway.open).toHaveLength(0);
});

test('a request for a topic this client does not know is refused at once', () => {
  const {gateway, subscriptions} = hub();
  const ends: (string | undefined)[] = [];
  const unknown = create(SubscribeRequestSchema);
  subscriptions.observe(
    HOST,
    unknown,
    undefined,
    () => {},
    error => ends.push(error),
  );
  expect(ends).toEqual(['unknown subscription topic']);
  expect(gateway.streams).toHaveLength(0);
});
