import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {SubscribeRequestSchema, TopicFailureCode} from '@vibesys/proto';
import {type SubscriptionState, type SubscriptionUpdate, TOPICS} from '@vibesys/subscription';
import fc from 'fast-check';
import {hubRig, settle} from '../testing/hub-rig.js';
import {SUBSCRIPTION_LINGER_MS, SubscriptionFailed} from './subscriptions.js';

const HOST = 'ssh:host-a';
const topic = TOPICS.machineStats;

const hub = hubRig;
/** A bound a test reader gives its first read; the subject is that it ends, not how long. */
const BOUND_MS = 30_000;

test('an observer that throws is reported and does not stop the others or the machine', () => {
  const {gateway, subscriptions, reported} = hub();
  const heard: SubscriptionState<unknown>[] = [];
  let broken = false;
  subscriptions.observe(HOST, topic, () => {
    if (broken) throw new Error('screen bug');
  });
  broken = true;
  subscriptions.observe(HOST, topic, state => heard.push(state));
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
  subscriptions.relay(
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
  const value = live.subscriptions.first(HOST, topic, wanted.signal, BOUND_MS);
  live.gateway.streams[0]?.reached('runs');
  expect(live.gateway.open).toHaveLength(1);
  void value.catch(() => {});

  const refused = hub();
  const never = refused.subscriptions.first(HOST, topic, new AbortController().signal, BOUND_MS);
  refused.gateway.streams[0]?.reached('runs');
  refused.gateway.streams[0]?.send({
    case: 'failed',
    value: {code: TopicFailureCode.UNSUPPORTED, detail: ''},
  });
  await expect(never).rejects.toEqual(new SubscriptionFailed('unsupported', false));
  await settle();
  refused.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(refused.gateway.open).toHaveLength(0);
  expect(refused.clock.pending).toBe(0);

  wanted.abort(new Error('screen left'));
  await expect(value).rejects.toThrow('screen left');
  await settle();
  live.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(live.gateway.open).toHaveLength(0);
});

test('property: a first read ends at its bound whatever the stream is still trying, and leaves nothing running', async () => {
  await fc.assert(
    // Below the stream's own end, which is `failureBoundMs`: past it the read has failed anyway.
    fc.asyncProperty(fc.integer({min: 1, max: 600_000}), async boundMs => {
      const {clock, gateway, subscriptions} = hub();
      let outcome: unknown = 'pending';
      const read = subscriptions.first(HOST, topic, new AbortController().signal, boundMs);
      read.then(
        value => (outcome = value),
        error => (outcome = error),
      );
      // The host dials and never gives a frame: the stream loses, waits, and tries again.
      gateway.streams[0]?.fail();
      clock.advanceBy(boundMs - 1);
      await settle();
      expect(outcome).toBe('pending');
      clock.advanceBy(1);
      await settle();
      expect(outcome).toEqual(new SubscriptionFailed('no_first_frame', true));
      // The reader is gone; the subscription's own retries end with its linger, and no timer is left.
      clock.advanceBy(SUBSCRIPTION_LINGER_MS);
      await settle();
      expect(gateway.open).toHaveLength(0);
      expect(clock.pending).toBe(0);
    }),
    {numRuns: 25},
  );
});

test('a request for a topic this client does not know is refused at once', () => {
  const {gateway, subscriptions} = hub();
  const ends: (string | undefined)[] = [];
  const unknown = create(SubscribeRequestSchema);
  subscriptions.relay(
    HOST,
    unknown,
    undefined,
    () => {},
    error => ends.push(error),
  );
  expect(ends).toEqual(['unknown subscription topic']);
  expect(gateway.streams).toHaveLength(0);
});
