/**
 * The contract every topic passes through the hub: whatever its values, a client sees a snapshot
 * then changes, a replacement snapshot with no ghost of the last, an answer within a bound when
 * nothing ever arrives, a lease and a value bound, and a stream that stops with its last observer.
 * It runs under `bun:test`, so the barrel does not re-export it; `subscriptions.test.ts` imports it
 * by path for every topic the registry lists.
 */
import {describe, expect, test} from 'bun:test';
import type {TopicEntry} from '@vibesys/proto';
import {subscribeTiming, TopicFailureCode} from '@vibesys/proto';
import {
  backoffMs,
  failureBoundMs,
  OFFLINE_AFTER,
  type SubscriptionState,
  type Topic,
} from '@vibesys/subscription';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {Subscriptions} from '../shell/subscriptions.js';
import {GATEWAY_FIRST_CONNECT_BUDGET_MS} from '../state/gateway.js';
import {type FakeSubscribeStream, FakeSubscribeStreams} from './fake-subscribe-streams.js';

const HOST = 'ssh:host-a';
const BEAT = subscribeTiming.heartbeatMs;

/** What a topic's author supplies: values that stay inside the topic's bounds. */
export interface TopicCase<S> {
  readonly topic: Topic<S>;
  /**
   * Two snapshots and the changes between them, as entries. The changes touch keys of the first
   * snapshot or add keys within the topic's `maxKeys`.
   */
  readonly scenario: fc.Arbitrary<Scenario>;
}

interface Scenario {
  readonly first: readonly TopicEntry[];
  readonly changes: readonly TopicEntry[];
  readonly second: readonly TopicEntry[];
}

/** The hub over fake time and a scripted gateway. */
class Rig<S> {
  readonly clock = new FakeClock();
  readonly gateway = new FakeSubscribeStreams();
  readonly hub = new Subscriptions(this.gateway, this.clock.schedule, () => this.clock.now());
  readonly states: SubscriptionState<S>[] = [];

  constructor(readonly topic: Topic<S>) {}

  watch(key?: string): () => void {
    return this.hub.watch(HOST, this.topic, state => this.states.push(state), key);
  }

  get state(): SubscriptionState<S> {
    const state = this.states.at(-1);
    if (state === undefined) throw new Error('nothing watched');
    return state;
  }

  get stream(): FakeSubscribeStream {
    const stream = this.gateway.streams.at(-1);
    if (stream === undefined) throw new Error('the hub opened no stream');
    return stream;
  }

  /** Let the outcome of a stream that ended reach the machine. */
  async settle(): Promise<void> {
    for (let drain = 0; drain < 5; drain += 1) await Promise.resolve();
  }

  snapshot(entries: readonly TopicEntry[]): void {
    this.stream.reached('runs');
    for (const entry of entries) this.stream.send({case: 'entry', value: entry});
    this.stream.send({case: 'synced', value: {}});
  }

  beat(): void {
    this.stream.send({case: 'heartbeat', value: {}});
  }

  /** What the topic makes of entries sent in order, the last of a key winning. */
  expected(...batches: readonly (readonly TopicEntry[])[]): S | null {
    const map = new Map<string, TopicEntry>();
    for (const batch of batches) for (const entry of batch) map.set(entry.key, entry);
    return this.topic.fold(map);
  }
}

async function snapshotThenChanges<S>(topic: Topic<S>, scenario: Scenario): Promise<void> {
  const {first, changes, second} = scenario;
  const rig = new Rig(topic);
  rig.watch();
  rig.snapshot(first);
  expect(rig.state).toMatchObject({kind: 'live', value: rig.expected(first)});
  for (const [index, change] of changes.entries()) {
    rig.stream.send({case: 'entry', value: change});
    const value = rig.expected(first, changes.slice(0, index + 1));
    expect(rig.state).toMatchObject({kind: 'live', value});
  }
  rig.stream.fail();
  await rig.settle();
  expect(rig.state.kind).toBe('stale');
  rig.clock.advanceBy(backoffMs(1));
  expect(rig.gateway.open).toHaveLength(1);
  // Until the new snapshot is whole, the old value stays on screen.
  rig.stream.reached('runs');
  expect(rig.state).toMatchObject({kind: 'stale', value: rig.expected(first, changes)});
  rig.snapshot(second);
  expect(rig.state).toMatchObject({kind: 'live', value: rig.expected(second)});
}

type Behavior = 'hang' | 'steps' | 'stream' | 'beats' | 'fail';
const BEHAVIORS: readonly Behavior[] = ['hang', 'steps', 'stream', 'beats', 'fail'];

/** How the layers below treat one attempt that never brings a snapshot. */
function treat<S>(rig: Rig<S>, stream: FakeSubscribeStream, behavior: Behavior): void {
  if (behavior === 'steps') stream.reached('handshake');
  if (behavior === 'stream' || behavior === 'beats') stream.reached('runs');
  if (behavior === 'beats') rig.beat();
  if (behavior === 'fail') stream.fail();
}

async function failsWithinTheBound<S>(topic: Topic<S>, behaviors: readonly Behavior[]) {
  const rig = new Rig(topic);
  rig.watch();
  const bound = failureBoundMs(topic, GATEWAY_FIRST_CONNECT_BUDGET_MS);
  let handled = 0;
  while (rig.clock.now() <= bound) {
    await rig.settle();
    const stream = rig.gateway.streams[handled];
    if (stream?.open) {
      treat(rig, stream, behaviors[handled % behaviors.length] ?? 'hang');
      handled += 1;
    }
    expect(rig.gateway.open.length).toBeLessThanOrEqual(1);
    rig.clock.advanceBy(Math.min(BEAT, 7_919));
  }
  await rig.settle();
  expect(rig.state).toMatchObject({kind: 'failed', retrying: true});
}

/** Beats for as long as the value allows, then the first one past it. */
function beatsPastTheValueBound<S>(rig: Rig<S>, bound: number): void {
  for (let at = BEAT; at <= bound; at += BEAT) {
    rig.clock.advanceBy(BEAT);
    rig.beat();
    expect(rig.state.kind).toBe('live');
  }
  rig.clock.advanceBy(BEAT);
  rig.beat();
  expect(rig.state).toMatchObject({kind: 'stale', reason: 'outdated'});
}

function beatsThenSilence<S>(rig: Rig<S>): void {
  for (let beat = 0; beat < 50; beat += 1) {
    rig.clock.advanceBy(BEAT);
    rig.beat();
  }
  expect(rig.state.kind).toBe('live');
  rig.clock.advanceBy(subscribeTiming.leaseMs);
  expect(rig.state).toMatchObject({kind: 'stale', reason: 'silent'});
}

function leaseAndValueBound<S>(topic: Topic<S>, scenario: Scenario): void {
  const rig = new Rig(topic);
  rig.watch();
  rig.snapshot(scenario.first);
  // Heartbeats keep a stream live up to the value bound, or without end if the topic has none.
  if (topic.policy.staleAfterMs > 0) beatsPastTheValueBound(rig, topic.policy.staleAfterMs);
  else beatsThenSilence(rig);
  expect(rig.gateway.open).toHaveLength(0);
}

function lastObserverStopsTheStream<S>(topic: Topic<S>, scenario: Scenario): void {
  const rig = new Rig(topic);
  const one = rig.watch();
  const two = rig.watch();
  expect(rig.gateway.streams).toHaveLength(1);
  rig.snapshot(scenario.first);
  one();
  expect(rig.gateway.open).toHaveLength(1);
  two();
  expect(rig.gateway.open).toHaveLength(0);
  expect(rig.stream.signal.aborted).toBe(true);
  expect(rig.clock.pending).toBe(0);
  // A new observer starts a fresh stream, not the old one's leftovers.
  rig.watch();
  expect(rig.gateway.streams).toHaveLength(2);
  expect(rig.state.kind).toBe('connecting');
}

function lateObserverIsToldTheState<S>(topic: Topic<S>, scenario: Scenario): void {
  const rig = new Rig(topic);
  rig.watch();
  rig.snapshot(scenario.first);
  const late: SubscriptionState<S>[] = [];
  rig.hub.watch(HOST, topic, state => late.push(state));
  expect(rig.gateway.streams).toHaveLength(1);
  expect(late.at(-1)).toMatchObject({kind: 'live', value: rig.expected(scenario.first)});
  // A watcher of one key is told that key's snapshot, which the topic may or may not accept alone.
  const key = scenario.first[0]?.key;
  if (key === undefined) return;
  const own = scenario.first.filter(entry => entry.key === key);
  const keyed: SubscriptionState<S>[] = [];
  rig.hub.watch(HOST, topic, state => keyed.push(state), key);
  const alone = topic.fold(new Map(own.map(entry => [entry.key, entry])));
  expect(keyed.at(-1)?.kind).toBe(alone === null ? 'failed' : 'live');
}

function refusals<S>(topic: Topic<S>): void {
  const rig = new Rig(topic);
  rig.watch();
  rig.stream.reached('runs');
  rig.stream.send({case: 'failed', value: {code: TopicFailureCode.UNSUPPORTED, detail: 'x'}});
  expect(rig.state).toEqual({kind: 'failed', reason: 'unsupported', retrying: false});
  rig.clock.advanceBy(60 * 60_000);
  expect(rig.gateway.streams).toHaveLength(1);
  rig.hub.retry(HOST);
  expect(rig.gateway.streams).toHaveLength(2);

  for (const code of [
    TopicFailureCode.SOURCE_FAILED,
    TopicFailureCode.RESOURCE_EXHAUSTED,
    TopicFailureCode.INTERNAL,
  ]) {
    const other = new Rig(topic);
    other.watch();
    other.stream.reached('runs');
    other.stream.send({case: 'failed', value: {code, detail: ''}});
    expect(other.state).toMatchObject({kind: 'connecting', step: 'stream', attempt: 1});
    other.clock.advanceBy(backoffMs(1));
    expect(other.gateway.open).toHaveLength(1);
  }
}

function removedHost<S>(topic: Topic<S>): void {
  const rig = new Rig(topic);
  rig.watch();
  rig.stream.reached('runs');
  rig.hub.closeHost(HOST);
  expect(rig.state).toEqual({kind: 'failed', reason: 'closed', retrying: false});
  expect(rig.gateway.open).toHaveLength(0);
  expect(rig.clock.pending).toBe(0);
}

async function offlineAfterFiveLosses<S>(topic: Topic<S>): Promise<void> {
  const rig = new Rig(topic);
  rig.watch();
  for (let failure = 1; failure <= OFFLINE_AFTER; failure += 1) {
    rig.stream.fail();
    await rig.settle();
    if (failure < OFFLINE_AFTER) rig.clock.advanceBy(backoffMs(failure));
  }
  expect(rig.state).toMatchObject({kind: 'failed', retrying: true});
  expect(rig.gateway.open).toHaveLength(0);
  rig.clock.advanceBy(30_000);
  expect(rig.gateway.open).toHaveLength(1);
}

export function topicContract<S>(each: TopicCase<S>): void {
  const {topic, scenario} = each;
  const runs = {numRuns: 20};
  describe(`topic ${topic.arm}`, () => {
    test('a snapshot then changes is the value, and the next stream replaces it whole', async () => {
      await fc.assert(fc.asyncProperty(scenario, value => snapshotThenChanges(topic, value)));
    });
    test('with nothing ever arriving, it answers failed within the bound and holds no stream', async () => {
      const behaviors = fc.array(fc.constantFrom(...BEHAVIORS), {minLength: 1, maxLength: 12});
      await fc.assert(
        fc.asyncProperty(behaviors, got => failsWithinTheBound(topic, got)),
        runs,
      );
    });
    test('a silent stream goes stale at the lease, and an old value does so though beats continue', () => {
      fc.assert(
        fc.property(scenario, value => leaseAndValueBound(topic, value)),
        runs,
      );
    });
    test('the last observer to leave stops the stream, drops the machine and leaves no timer', () => {
      fc.assert(
        fc.property(scenario, value => lastObserverStopsTheStream(topic, value)),
        runs,
      );
    });
    test('an observer that joins late is told the current state, and one of a key is told its key', () => {
      fc.assert(
        fc.property(scenario, value => lateObserverIsToldTheState(topic, value)),
        runs,
      );
    });
    test('an unsupported topic stops the retries and any other failure is retried', () => {
      refusals(topic);
    });
    test('removing the host tells its observers failed closed and stops its stream', () => {
      removedHost(topic);
    });
    test('five lost streams in a row are failed, which keeps trying', async () => {
      await offlineAfterFiveLosses(topic);
    });
  });
}
