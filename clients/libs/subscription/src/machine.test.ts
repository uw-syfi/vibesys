import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {subscribeTiming, TopicEntrySchema} from '@vibesys/proto';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  applyUpdate,
  backoffMs,
  controlOf,
  type Fold,
  failureBoundMs,
  INITIAL_WINDOW,
  OFFLINE_AFTER,
  replayUpdates,
  type StreamEvent,
  type SubscriptionUpdate,
  snapshotValue,
  stateOf,
  subscriptionMachine,
  type WindowState,
} from './index.js';
import {type FakeStream, Harness, LEASE_ONLY, statsEntry} from './testing/harness.js';

const LEASE = subscribeTiming.leaseMs;
const BEAT = subscribeTiming.heartbeatMs;

/** Let the promise outcomes of the streams reach the actor. */
async function settle(): Promise<void> {
  for (let drain = 0; drain < 5; drain += 1) await Promise.resolve();
}

async function live(harness: Harness, keys: readonly string[] = ['machine']): Promise<void> {
  await settle();
  harness.latest.snapshot(keys.map(key => statsEntry(key, harness.clock.now())));
  await settle();
}

test('it opens one stream, reports each step, and is live at the snapshot', async () => {
  const harness = new Harness();
  expect(harness.control).toEqual({kind: 'connecting', step: 'gateway', attempt: 0});
  harness.latest.reach('handshake');
  expect(harness.control).toEqual({kind: 'connecting', step: 'handshake', attempt: 0});
  harness.clock.advanceBy(1_000);
  await live(harness);
  expect(harness.control).toEqual({kind: 'live', since: 1_000});
  expect(harness.streams).toHaveLength(1);
  expect(harness.updates.map(update => update.kind)).toEqual(['begin', 'entry', 'synced']);
});

test('a stream that says nothing is lost at the open budget, then at the first-frame bound', async () => {
  const harness = new Harness(LEASE_ONLY, 60_000);
  harness.clock.advanceBy(59_999);
  expect(harness.control).toMatchObject({kind: 'connecting', attempt: 0});
  harness.clock.advanceBy(1);
  expect(harness.control).toEqual({kind: 'connecting', step: 'gateway', attempt: 1});
  expect(harness.open).toHaveLength(0);
  harness.clock.advanceBy(backoffMs(1));
  expect(harness.open).toHaveLength(1);
  harness.latest.reach('stream');
  // Heartbeats prove the stream, not a snapshot.
  for (let at = 0; at < LEASE_ONLY.policy.firstFrameMs; at += BEAT) {
    harness.latest.beat();
    harness.clock.advanceBy(Math.min(BEAT, LEASE_ONLY.policy.firstFrameMs - at));
  }
  // A stream that fails keeps showing how far it had come.
  expect(harness.control).toEqual({kind: 'connecting', step: 'stream', attempt: 2});
});

test('every frame renews the lease, and silence past it makes the value stale', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness, ['a', 'b']);
  for (let beat = 0; beat < 10; beat += 1) {
    harness.clock.advanceBy(LEASE - 1);
    harness.latest.beat();
  }
  expect(harness.control.kind).toBe('live');
  harness.clock.advanceBy(LEASE - 1);
  expect(harness.control.kind).toBe('live');
  harness.clock.advanceBy(1);
  expect(harness.control).toEqual({kind: 'stale', reason: 'silent', attempt: 1, valueAt: 0});
  expect(harness.open).toHaveLength(0);
});

test('heartbeats do not keep a sampled value fresh', async () => {
  const harness = new Harness();
  await live(harness);
  const age = harness.topic.policy.staleAfterMs;
  expect(age).toBeGreaterThanOrEqual(BEAT * 2);
  const beatsFor = (ms: number) => {
    for (let at = 0; at < ms; at += BEAT) {
      harness.clock.advanceBy(BEAT);
      harness.latest.beat();
    }
  };
  beatsFor(age - BEAT);
  const valueAt = harness.clock.now();
  harness.latest.entry(statsEntry('machine', 1));
  // A value inside the bound keeps it fresh, however long ago the snapshot was.
  beatsFor(age - BEAT);
  expect(harness.control.kind).toBe('live');
  // Beats alone cannot: the value is old at the bound, and says so then, with when it arrived.
  beatsFor(BEAT);
  expect(harness.control).toEqual({kind: 'stale', reason: 'outdated', attempt: 1, valueAt});
  expect(harness.open).toHaveLength(0);
});

test('the value bound is a timer: a wall clock that steps back cannot stretch it', async () => {
  const harness = new Harness();
  await live(harness);
  const age = harness.topic.policy.staleAfterMs;
  // Frames stamped by a clock that went back a day: the stamps are display, not a deadline.
  const back = harness.clock.now() - 24 * 60 * 60_000;
  for (let at = BEAT; at < age; at += BEAT) {
    harness.clock.advanceBy(BEAT);
    harness.latest.send({type: 'beat', at: back});
  }
  expect(harness.control.kind).toBe('live');
  harness.clock.advanceBy(BEAT);
  expect(harness.control).toMatchObject({kind: 'stale', reason: 'outdated'});
});

test('a topic without a value bound is held by its lease alone', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness);
  for (let beats = 0; beats < 100; beats += 1) {
    harness.clock.advanceBy(BEAT);
    harness.latest.beat();
  }
  expect(harness.control.kind).toBe('live');
});

test('data before the stream says it arrived is read, never dropped', async () => {
  const harness = new Harness();
  await settle();
  // The order the gateway keeps is reached, then data; here the data comes first.
  harness.latest.entry(statsEntry('machine', 1));
  expect(harness.control).toEqual({kind: 'connecting', step: 'stream', attempt: 0});
  harness.latest.synced();
  expect(harness.control.kind).toBe('live');
  expect(harness.updates.map(update => update.kind)).toEqual(['begin', 'entry', 'synced']);

  // A snapshot that is only its end is read as one too, and a topic that allows none is live.
  const empty = new Harness(LEASE_ONLY);
  await settle();
  empty.latest.synced();
  expect(empty.control.kind).toBe('live');
});

test('a new stream keeps the old value until its snapshot, which replaces it whole', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness, ['a', 'b']);
  harness.latest.fail();
  await settle();
  expect(harness.control).toEqual({kind: 'stale', reason: 'ended', attempt: 1, valueAt: 0});
  harness.clock.advanceBy(backoffMs(1));
  expect(harness.open).toHaveLength(1);
  harness.latest.reach('stream');
  harness.latest.entry(statsEntry('a', 2));
  expect([...(harness.actor.getSnapshot().context.fold.map ?? new Map()).keys()]).toEqual([
    'a',
    'b',
  ]);
  harness.latest.synced();
  expect(harness.control.kind).toBe('live');
  expect([...(harness.actor.getSnapshot().context.fold.map ?? new Map()).keys()]).toEqual(['a']);
});

test('a stream that was left cannot speak for the next one', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness, ['a']);
  const old = harness.latest;
  harness.clock.advanceBy(LEASE);
  harness.clock.advanceBy(backoffMs(1));
  expect(harness.streams).toHaveLength(2);
  harness.latest.reach('stream');
  old.entry(statsEntry('ghost', 3));
  old.synced();
  harness.latest.entry(statsEntry('a', 4));
  harness.latest.synced();
  expect([...(harness.actor.getSnapshot().context.fold.map ?? new Map()).keys()]).toEqual(['a']);
  expect(harness.control).toEqual({kind: 'live', since: harness.clock.now()});
});

test('five failures in a row are a failed answer that keeps trying, until one stream is live', async () => {
  const harness = new Harness(LEASE_ONLY);
  for (let failure = 1; failure <= OFFLINE_AFTER; failure += 1) {
    await settle();
    harness.latest.fail();
    await settle();
    if (failure < OFFLINE_AFTER) {
      expect(harness.control).toMatchObject({kind: 'connecting', attempt: failure});
      harness.clock.advanceBy(backoffMs(failure));
    }
  }
  expect(harness.control).toEqual({kind: 'failed', reason: 'unreachable', retrying: true});
  expect(harness.open).toHaveLength(0);
  const opened = harness.streams.length;
  harness.clock.advanceBy(30_000);
  expect(harness.streams).toHaveLength(opened + 1);
  // Trying again does not flicker the answer.
  expect(harness.control).toEqual({kind: 'failed', reason: 'unreachable', retrying: true});
  await live(harness);
  expect(harness.control.kind).toBe('live');
});

test('the numbers are the contract: five losses in a row, the backoffs of 1, 2, 4 and 8 seconds, then every 30', async () => {
  const harness = new Harness(LEASE_ONLY);
  const backoffs = [1_000, 2_000, 4_000, 8_000];
  for (const [index, wait] of backoffs.entries()) {
    await settle();
    harness.latest.fail();
    await settle();
    expect(harness.control).toMatchObject({kind: 'connecting', attempt: index + 1});
    harness.clock.advanceBy(wait - 1);
    expect(harness.open).toHaveLength(0);
    harness.clock.advanceBy(1);
    expect(harness.open).toHaveLength(1);
  }
  harness.latest.fail();
  await settle();
  // The fifth loss is the answer, and a host that keeps failing is tried every 30 seconds.
  expect(harness.control).toEqual({kind: 'failed', reason: 'unreachable', retrying: true});
  harness.clock.advanceBy(29_999);
  expect(harness.open).toHaveLength(0);
  harness.clock.advanceBy(1);
  expect(harness.open).toHaveLength(1);
});

test('losses do not accumulate across recoveries: five in a row, not five over a week', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness);
  for (let loss = 1; loss <= 3; loss += 1) {
    harness.latest.fail();
    await settle();
    harness.clock.advanceBy(backoffMs(loss));
  }
  await live(harness);
  expect(harness.control.kind).toBe('live');
  harness.latest.fail();
  await settle();
  // One loss since it was last live, so it backs off one second, not eight.
  expect(harness.control).toMatchObject({kind: 'stale', reason: 'ended', attempt: 1});
  harness.clock.advanceBy(1_000);
  expect(harness.open).toHaveLength(1);
});

test('an unsupported topic or a broken frame stops the retries, and only retry starts them again', async () => {
  for (const [reason, break_] of [
    ['unsupported', (stream: FakeStream) => stream.send({type: 'refused', reason: 'unsupported'})],
    // An entry that no topic owns is a broken frame.
    ['invalid_frame', (stream: FakeStream) => stream.entry(create(TopicEntrySchema, {key: 'x'}))],
  ] as const) {
    const harness = new Harness(LEASE_ONLY);
    await settle();
    harness.latest.reach('stream');
    break_(harness.latest);
    expect(harness.control).toEqual({kind: 'failed', reason, retrying: false});
    expect(harness.open).toHaveLength(0);
    harness.clock.advanceBy(10 * 60_000);
    expect(harness.streams).toHaveLength(1);
    harness.actor.send({type: 'retry'});
    expect(harness.streams).toHaveLength(2);
    expect(harness.control).toMatchObject({kind: 'connecting'});
    // A new stream starts clean, even after a broken one.
    harness.latest.snapshot([statsEntry('a', 1)]);
    expect(harness.control.kind).toBe('live');
  }
});

test('a topic failure other than unsupported is retried like a lost stream', async () => {
  for (const reason of ['source_failed', 'resource_exhausted', 'internal'] as const) {
    const harness = new Harness(LEASE_ONLY);
    await live(harness);
    harness.latest.send({type: 'refused', reason});
    expect(harness.control).toMatchObject({kind: 'stale', reason, attempt: 1});
    harness.clock.advanceBy(backoffMs(1));
    expect(harness.open).toHaveLength(1);
  }
});

test('waking checks a live stream within a beat and retries a waiting one at once', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness);
  harness.actor.send({type: 'wake'});
  harness.clock.advanceBy(BEAT);
  harness.latest.beat();
  harness.clock.advanceBy(LEASE - 1);
  expect(harness.control.kind).toBe('live');

  harness.actor.send({type: 'wake'});
  harness.clock.advanceBy(BEAT + 2_000);
  expect(harness.control).toMatchObject({kind: 'stale', reason: 'silent', attempt: 1});
  harness.actor.send({type: 'wake'});
  expect(harness.open).toHaveLength(1);
  expect(harness.control).toMatchObject({kind: 'stale', attempt: 0});
});

test('closing is final: observers hear failed closed and nothing runs', async () => {
  const harness = new Harness(LEASE_ONLY);
  await live(harness);
  harness.actor.send({type: 'close'});
  expect(harness.control).toEqual({kind: 'failed', reason: 'closed', retrying: false});
  expect(harness.open).toHaveLength(0);
  expect(harness.clock.pending).toBe(0);
  expect(harness.actor.getSnapshot().status).toBe('done');
});

type Attempt = 'hang' | 'reach-gateway' | 'reach-stream' | 'beats' | 'fail' | 'finish';

/** How the layers below can treat one attempt that never produces a snapshot. */
const BEHAVIORS: Readonly<
  Record<Attempt, (stream: FakeStream, harness: Harness, end: number) => void>
> = {
  hang: () => {},
  'reach-gateway': stream => stream.reach('handshake'),
  'reach-stream': stream => stream.reach('stream'),
  fail: stream => stream.fail(),
  finish: stream => stream.finish(),
  beats: (stream, harness, end) => {
    stream.reach('stream');
    for (
      let beat = 0;
      beat < 40 && harness.clock.now() < end && harness.open.includes(stream);
      beat += 1
    ) {
      stream.beat();
      harness.clock.advanceBy(BEAT);
    }
  },
};
const ATTEMPTS = Object.keys(BEHAVIORS) as Attempt[];

/** Run one seed to past the bound, letting each new stream be treated as the seed picks. */
async function runToBound(seed: number): Promise<Harness> {
  const random = new Seeded(seed);
  const first = random.pick([1_000, 15_000]);
  const topic = {...LEASE_ONLY, policy: {...LEASE_ONLY.policy, firstFrameMs: first}};
  const openBudget = random.pick([1_000, 60_000, 360_000]);
  const harness = new Harness(topic, openBudget);
  const end = failureBoundMs(topic, openBudget) + 1;
  let handled = 0;
  while (harness.clock.now() < end) {
    await settle();
    const stream = harness.streams[handled];
    if (stream !== undefined && !stream.aborted) {
      handled += 1;
      BEHAVIORS[random.pick(ATTEMPTS)](stream, harness, end);
    }
    expect(harness.open.length).toBeLessThanOrEqual(1);
    harness.clock.advanceBy(random.pick([1, 997, 5_000, 29_999]));
  }
  await settle();
  return harness;
}

test('property: with no snapshot ever, any behavior of the layers below ends in a failed answer within the bound', async () => {
  for (const seed of SEEDS.slice(0, 60)) {
    const harness = await runToBound(seed);
    expect(harness.control).toMatchObject({kind: 'failed', retrying: true});
  }
});

/** A random event from outside the machine, as the layers below and the owner can raise it. */
function outsideEvent(
  random: Seeded,
): StreamEvent | {type: 'retry'} | {type: 'wake'} | {type: 'close'} {
  const key = random.pick(['a', 'b', 'c']);
  const at = random.int(1_000);
  return random.pick([
    {type: 'reached' as const, step: random.pick(['gateway', 'handshake', 'stream'] as const)},
    {type: 'beat' as const, at},
    {type: 'entry' as const, entry: statsEntry(key, random.int(100)), at},
    {type: 'removed' as const, key, at},
    {type: 'synced' as const, at},
    {
      type: 'refused' as const,
      reason: random.pick(['source_failed', 'unsupported', 'internal'] as const),
    },
    {type: 'retry' as const},
    {type: 'wake' as const},
  ]);
}

test('the machine keeps its streams, delays and answers in step over every seeded interleaving', () => {
  checkMachine({
    machine: subscriptionMachine,
    input: {topic: LEASE_ONLY, openBudgetMs: 60_000},
    event: (random, liveOps, snapshot) => {
      const settles = [
        ...liveOps.operations.map(operation => liveOps.done(operation)),
        ...liveOps.operations.map(operation => liveOps.error(operation, new Error('ended'))),
        ...liveOps.delays.map(delay => liveOps.fire(delay)),
      ];
      if (snapshot.status === 'done') return null;
      return random.bool()
        ? random.pick(settles.length > 0 ? settles : [{type: 'wake'}])
        : outsideEvent(random);
    },
    invariants: snapshot => {
      const problems: string[] = [];
      const control = controlOf(snapshot);
      const held = snapshot.context.fold.map !== null;
      if ((control.kind === 'live' || control.kind === 'stale') && !held)
        problems.push(`${control.kind} without a snapshot`);
      if (control.kind === 'live' && !snapshot.matches({streaming: 'live'}))
        problems.push('live outside the live state');
      if (
        control.kind === 'failed' &&
        control.retrying !== (snapshot.context.halted === null && !snapshot.matches('closed'))
      )
        problems.push('a failed answer disagrees with whether it retries');
      return problems;
    },
  });
});

const KEYS = ['a', 'b', 'c'] as const;

/** One random thing the gateway does to the machine's current stream. */
function gatewayDoes(random: Seeded, stream: FakeStream, step: number): void {
  const choice = random.int(6);
  const key = random.pick(KEYS);
  if (choice === 0)
    stream.snapshot(KEYS.filter(() => random.bool()).map(each => statsEntry(each, step)));
  else if (choice === 1) stream.entry(statsEntry(key, step));
  else if (choice === 2) stream.removed(key);
  else if (choice === 3) stream.fail();
  else if (choice === 4) stream.beat();
}

function keysOf(map: ReadonlyMap<string, unknown> | null): string[] {
  return [...(map ?? new Map()).keys()].sort();
}

function replayed(fold: Fold, control: SubscriptionUpdate): WindowState {
  let window = INITIAL_WINDOW;
  for (const update of [...replayUpdates(fold), control])
    window = applyUpdate(LEASE_ONLY, window, update);
  return window;
}

/** Windows that fold what the machine emits: one from the start, one joining at `joinAt`. */
class Windows {
  early: WindowState = INITIAL_WINDOW;
  late: WindowState | null = null;
  #seen = 0;

  constructor(
    readonly harness: Harness,
    readonly joinAt: number,
  ) {}

  absorb(step: number): void {
    const {harness} = this;
    const {fold} = harness.actor.getSnapshot().context;
    // A window hears data first and control after, as main sends them.
    const control: SubscriptionUpdate = {kind: 'control', control: harness.control};
    const sent = [...harness.updates.slice(this.#seen), control];
    this.#seen = harness.updates.length;
    for (const update of sent) this.early = applyUpdate(LEASE_ONLY, this.early, update);
    if (step === this.joinAt) this.late = replayed(fold, control);
    else if (this.late !== null)
      for (const update of sent) this.late = applyUpdate(LEASE_ONLY, this.late, update);
  }

  check(): void {
    const {fold} = this.harness.actor.getSnapshot().context;
    for (const window of this.late === null ? [this.early] : [this.early, this.late]) {
      expect(window.fold.broken).toBe(fold.broken);
      expect(keysOf(window.fold.map)).toEqual(keysOf(fold.map));
      expect(window.control).toEqual(this.harness.control);
      const state = stateOf(LEASE_ONLY, window);
      expect(state.kind).toBe(this.harness.control.kind);
      if (state.kind === 'live' || state.kind === 'stale')
        expect(keysOf(state.value)).toEqual(keysOf(snapshotValue(LEASE_ONLY, fold)));
    }
  }
}

test("property: a window that folds what the machine emits holds the machine's own snapshot, from the start or from any later join", async () => {
  for (const seed of SEEDS.slice(0, 80)) {
    const random = new Seeded(seed);
    const harness = new Harness(LEASE_ONLY);
    const windows = new Windows(harness, random.int(60));
    for (let step = 0; step < 80; step += 1) {
      await settle();
      const stream = harness.streams.at(-1);
      if (stream !== undefined && harness.open.includes(stream)) gatewayDoes(random, stream, step);
      harness.clock.advanceBy(random.pick([1_000, 10_000, 31_000]));
      await settle();
      windows.absorb(step);
      windows.check();
    }
  }
});
