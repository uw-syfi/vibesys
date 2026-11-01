import {expect, test} from 'bun:test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeClock} from '@vibesys/transport/testing';
import {getInitialSnapshot, getNextSnapshot, type SnapshotFrom} from 'xstate';
import {
  gatewayRunMachine,
  isCatchUpBatch,
  PROJECTION_PACE_MS,
  paintedSkeleton,
  projectSkeleton,
  skeletonScreenState,
} from '../../state/index.js';
import {pacedSnapshots, type Snapshots} from './paced-snapshots.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
type Snapshot = SnapshotFrom<typeof gatewayRunMachine>;

/** The loader's side: a run read batch by batch, published to whoever subscribed. */
function loader() {
  const records = storeRunFixture('short', 60, 1).records;
  const listeners = new Set<(snapshot: Snapshot) => void>();
  let snapshot = getInitialSnapshot(gatewayRunMachine, {seed: SEED});
  const source: Snapshots<Snapshot> = {
    subscribe: listener => {
      listeners.add(listener);
      return {unsubscribe: () => listeners.delete(listener)};
    },
  };
  return {
    source,
    records,
    /** Publish the run read through its first `through` records. */
    publish(through: number): Snapshot {
      const complete = through === records.length;
      snapshot = getNextSnapshot(gatewayRunMachine, snapshot, {
        type: 'loaded',
        load: {
          skeleton: foldSkeleton(seedSkeleton(), records.slice(0, through)),
          nextSeq: BigInt(through + 1),
          committedSeq: BigInt(records.length),
          complete,
          windowFrom: undefined,
          backfill: undefined,
          failuresBefore: undefined,
        },
        progress: complete
          ? {phase: 'complete'}
          : {phase: 'history', through: BigInt(through), committed: BigInt(records.length)},
        failure: undefined,
        terminal: false,
        fresh: false,
      });
      for (const listener of [...listeners]) listener(snapshot);
      return snapshot;
    },
    subscribers: () => listeners.size,
  };
}

/** What the page does with each snapshot it is given: both projections of the skeleton. */
function projecting() {
  const throughs: bigint[] = [];
  return {
    throughs,
    deliver: (snapshot: Snapshot) => {
      const skeleton = paintedSkeleton(snapshot.context.load);
      if (skeleton === undefined) return;
      const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
      skeletonScreenState(SEED, skeleton, whole, snapshot.context.ui);
      throughs.push(skeleton.throughSeq ?? 0n);
    },
  };
}

test('a catching-up read is projected once per tick and its completion at once', () => {
  const clock = new FakeClock();
  const read = loader();
  const page = projecting();
  const stop = pacedSnapshots(
    read.source,
    clock.schedule,
    (snapshot, previous) => isCatchUpBatch(previous?.context, snapshot.context),
    page.deliver,
  );
  for (let through = 3; through <= 30; through += 3) {
    read.publish(through);
    clock.advanceBy(10);
  }
  // Ten batches inside one tick: the first was projected when it arrived, the rest wait.
  expect(page.throughs).toEqual([3n]);
  clock.advanceBy(PROJECTION_PACE_MS);
  expect(page.throughs).toEqual([3n, 30n]);
  read.publish(45);
  read.publish(read.records.length);
  expect(page.throughs).toEqual([3n, 30n, BigInt(read.records.length)]);
  clock.advanceBy(10 * PROJECTION_PACE_MS);
  expect(page.throughs).toHaveLength(3);
  stop();
});

test('stopping cancels the tick and the subscription', () => {
  const clock = new FakeClock();
  const read = loader();
  const page = projecting();
  const stop = pacedSnapshots(
    read.source,
    clock.schedule,
    (snapshot, previous) => isCatchUpBatch(previous?.context, snapshot.context),
    page.deliver,
  );
  read.publish(3);
  read.publish(6);
  stop();
  expect(read.subscribers()).toBe(0);
  clock.advanceBy(10 * PROJECTION_PACE_MS);
  read.publish(9);
  expect(page.throughs).toEqual([3n]);
});
