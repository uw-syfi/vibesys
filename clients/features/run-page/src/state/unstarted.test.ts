import {expect, test} from 'bun:test';
import {fromBinary} from '@bufbuild/protobuf';
import {ClientReplaySchema, RecordSchema, type Record as StoreRecord} from '@vibesys/proto';
import {FIXTURE_NAMES, fixtureNamed} from '@vibesys/run-fixtures';
import {derivedReplay} from '@vibesys/run-fixtures/derived';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {
  headerView,
  projectSkeleton,
  runPageInitial,
  runPhase,
  skeletonScreenState,
  unstartedRun,
} from './index.js';

/**
 * Which runs the Run screen states as never started (`unstartedRun`), as the gateway reads them:
 * over every committed fixture, only the run that failed while setting up, whose statement carries
 * its whole reason while the header keeps none of it. A run that did any work keeps its charts and
 * faces, and a run still setting up is never stated as one that never started.
 */
const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

function recordsOf(name: string): StoreRecord[] {
  const replay = fromBinary(ClientReplaySchema, derivedReplay('shared', name));
  return replay.batches.flatMap(batch =>
    batch.records.map(bytes => fromBinary(RecordSchema, bytes)),
  );
}

function screenOf(records: readonly StoreRecord[]) {
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  return skeletonScreenState(SEED, skeleton, whole, runPageInitial(SEED));
}

test('only the run that failed while setting up is stated as never started', () => {
  for (const name of FIXTURE_NAMES) {
    const state = screenOf(recordsOf(name));
    expect({name, unstarted: unstartedRun(state) !== null}).toEqual({
      name,
      unstarted: name === 'failed-at-setup',
    });
  }
});

test('the statement carries the whole reason, and the header none of it', () => {
  const state = screenOf(recordsOf('failed-at-setup'));
  expect(unstartedRun(state)?.reason).toBe(fixtureNamed('failed-at-setup').end?.reason ?? '');
  const header = headerView(state);
  expect(header.ended).toBeNull();
  expect(header.resume.shown).toBe(false);
});

test('a run still setting up is never stated as one that never started', () => {
  const records = recordsOf('failed-at-setup');
  for (let length = 0; length <= records.length; length += 1) {
    const state = screenOf(records.slice(0, length));
    if (unstartedRun(state) !== null)
      expect({length, phase: runPhase(state).kind}).toEqual({length, phase: 'ended'});
  }
  expect(unstartedRun(screenOf(records))).not.toBeNull();
});
