import {expect, test} from 'bun:test';
import type {HostKey, HostListing} from '@vibesys/hosts';
import {RunLiveness, TopicFailureCode} from '@vibesys/proto';
import {backoffMs, OFFLINE_AFTER, runs as runsTopic} from '@vibesys/subscription';
import fc from 'fast-check';
import {LISTING_FAILURE_WORDS} from '../run-listing.js';
import {announce, announceChange, hubRig, settle} from '../testing/index.js';
import {hostRuns} from './host-runs.js';
import {SUBSCRIPTION_LINGER_MS, SubscriptionFailed} from './subscriptions.js';

const HOST = 'ssh:host-a' as HostKey;
const run = (runId: string, liveness: RunLiveness = RunLiveness.ALIVE) => ({
  runId,
  projectRoot: '/work/project',
  liveness,
});

function watching() {
  const rig = hubRig();
  const runs = hostRuns(rig.subscriptions);
  const listings: HostListing[] = [];
  const release = runs.observe(HOST, listing => listings.push(listing));
  return {...rig, runs, listings, release, last: () => listings.at(-1)};
}

function listed(listing: HostListing | undefined): string[] {
  if (listing?.kind !== 'listed') throw new Error(`not listed: ${JSON.stringify(listing)}`);
  return [...listing.live, ...listing.ended].map(each => each.runId).sort();
}

test('a host lists nothing until its snapshot is whole, then lists exactly its runs', () => {
  const {gateway, last} = watching();
  expect(last()).toMatchObject({
    kind: 'unavailable',
    step: 'gateway',
    connection: {kind: 'connecting'},
  });
  gateway.streams[0]?.reached('handshake');
  expect(last()).toMatchObject({kind: 'unavailable', step: 'handshake'});
  gateway.streams[0]?.reached('runs');
  expect(last()).toMatchObject({kind: 'unavailable', step: 'runs'});
  announce(gateway, HOST, [run('a'), run('b', RunLiveness.ENDED)]);
  expect(listed(last())).toEqual(['a', 'b']);
});

test('a run that appears, finishes and is removed reaches the listing as it happens', () => {
  const {gateway, last} = watching();
  announce(gateway, HOST, [run('a')]);
  announceChange(gateway, HOST, run('b'));
  expect(listed(last())).toEqual(['a', 'b']);
  announceChange(gateway, HOST, run('a', RunLiveness.ENDED));
  expect(last()).toMatchObject({kind: 'listed', live: [{runId: 'b'}], ended: [{runId: 'a'}]});
  announceChange(gateway, HOST, {runId: 'b', removed: true});
  expect(listed(last())).toEqual(['a']);
});

test('a run removed while the stream was down is gone from the next snapshot, with no ghost', async () => {
  const {gateway, clock, last} = watching();
  announce(gateway, HOST, [run('a'), run('b')]);
  gateway.streams[0]?.fail();
  await settle();
  // The old runs are not shown as a listing while the host is not delivering.
  expect(last()).toMatchObject({
    kind: 'unavailable',
    connection: {kind: 'reconnecting', attempt: 1},
  });
  clock.advanceBy(backoffMs(1));
  announce(gateway, HOST, [run('a')]);
  expect(listed(last())).toEqual(['a']);
});

test('a host that keeps failing is offline in the words of its reason, and a retry tries at once', async () => {
  const {gateway, clock, runs, last} = watching();
  for (let failure = 1; failure <= OFFLINE_AFTER; failure += 1) {
    gateway.streams.at(-1)?.fail();
    await settle();
    if (failure < OFFLINE_AFTER) clock.advanceBy(backoffMs(failure));
  }
  expect(last()).toMatchObject({
    kind: 'unavailable',
    connection: {kind: 'offline', detail: LISTING_FAILURE_WORDS.unreachable},
  });
  expect(gateway.open).toHaveLength(0);
  runs.retry(HOST);
  expect(gateway.open).toHaveLength(1);
});

test('property: a window that joins at any point is told the step the others were told, where the host stopped', async () => {
  const STEPS = ['gateway', 'handshake', 'runs'] as const;
  const OPS = fc.oneof(
    fc.constantFrom(...STEPS).map(step => ({kind: 'reach', step}) as const),
    fc.constant({kind: 'fail'} as const),
    fc.constant({kind: 'retry'} as const),
    fc.constant({kind: 'snapshot'} as const),
  );
  await fc.assert(
    fc.asyncProperty(
      // The window joins at one of the operations: a join point past the last would never join.
      fc
        .array(OPS, {minLength: 1, maxLength: 30})
        .chain(ops => fc.tuple(fc.constant(ops), fc.nat({max: ops.length - 1}))),
      async ([ops, joinAt]) => {
        const {gateway, clock, runs, listings, release} = watching();
        const late: HostListing[] = [];
        let releaseLate: (() => void) | null = null;
        // What the late window was told last is what the first window was told last, step and all.
        const agree = (): void => {
          const first = listings.at(-1);
          const second = late.at(-1);
          if (first !== undefined && second !== undefined) expect(second).toEqual(first);
        };
        for (const [index, op] of ops.entries()) {
          if (index === joinAt) {
            releaseLate = runs.observe(HOST, listing => late.push(listing));
            agree();
          }
          const stream = gateway.streams.at(-1);
          if (op.kind === 'reach') stream?.reached(op.step);
          else if (op.kind === 'fail') stream?.fail();
          else if (op.kind === 'retry') runs.retry(HOST);
          else announce(gateway, HOST, [run('a')]);
          await settle();
          agree();
          clock.advanceBy(backoffMs(1));
          agree();
        }
        releaseLate?.();
        release();
      },
    ),
    {numRuns: 60},
  );
});

test('a host that cannot serve the topic is offline with that word and is not retried by itself', () => {
  const {gateway, clock, last} = watching();
  gateway.streams[0]?.reached('runs');
  gateway.streams[0]?.send({
    case: 'failed',
    value: {code: TopicFailureCode.UNSUPPORTED, detail: ''},
  });
  expect(last()).toMatchObject({
    connection: {kind: 'offline', detail: LISTING_FAILURE_WORDS.unsupported},
  });
  clock.advanceBy(60 * 60_000);
  expect(gateway.streams).toHaveLength(1);
});

test('observers of one host share a stream, and the last to leave stops it', () => {
  const rig = hubRig();
  const runs = hostRuns(rig.subscriptions);
  const releases = [1, 2, 3].map(() => runs.observe(HOST, () => {}));
  expect(rig.gateway.streams).toHaveLength(1);
  for (const release of releases.slice(0, 2)) release();
  rig.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(rig.gateway.open).toHaveLength(1);
  releases[2]?.();
  rig.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(rig.gateway.open).toHaveLength(0);
  expect(rig.clock.pending).toBe(0);
});

test('alive counts the runs the host says are alive, once it says so', async () => {
  const rig = hubRig();
  const runs = hostRuns(rig.subscriptions);
  const counted = runs.alive(HOST, new AbortController().signal);
  announce(rig.gateway, HOST, [run('a'), run('b'), run('c', RunLiveness.ENDED), run('d')]);
  expect(await counted).toBe(3);
});

test('alive says why a host did not answer, and never a count of none', async () => {
  const rig = hubRig();
  const runs = hostRuns(rig.subscriptions);
  const counted = runs.alive(HOST, new AbortController().signal);
  rig.gateway.streams[0]?.reached('runs');
  rig.gateway.streams[0]?.send({
    case: 'failed',
    value: {code: TopicFailureCode.UNSUPPORTED, detail: ''},
  });
  const failure = await counted.catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(SubscriptionFailed);
  expect(failure).toMatchObject({reason: 'unsupported', retrying: false});
});

test('alive on a host that never answers ends at the topic’s first-frame bound, not after its attempts run out', async () => {
  const rig = hubRig();
  const counted = hostRuns(rig.subscriptions).alive(HOST, new AbortController().signal);
  let outcome: unknown = 'pending';
  counted.then(
    value => (outcome = value),
    error => (outcome = error),
  );
  // The stream opens and the host gives no frame; every attempt after it would too.
  rig.gateway.streams[0]?.reached('handshake');
  rig.clock.advanceBy(runsTopic.policy.firstFrameMs - 1);
  await settle();
  expect(outcome).toBe('pending');
  rig.clock.advanceBy(1);
  await settle();
  expect(outcome).toBeInstanceOf(SubscriptionFailed);
  expect(outcome).toMatchObject({reason: 'no_first_frame', retrying: true});
  rig.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(rig.gateway.open).toHaveLength(0);
  expect(rig.clock.pending).toBe(0);
});

test('alive stops waiting when its caller does, and leaves no stream behind', async () => {
  const rig = hubRig();
  const abort = new AbortController();
  const counted = hostRuns(rig.subscriptions).alive(HOST, abort.signal);
  abort.abort(new Error('not wanted'));
  await expect(counted).rejects.toThrow('not wanted');
  rig.clock.advanceBy(SUBSCRIPTION_LINGER_MS);
  expect(rig.gateway.open).toHaveLength(0);
});
