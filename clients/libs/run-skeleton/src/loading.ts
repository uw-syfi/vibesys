import {fromBinary} from '@bufbuild/protobuf';
import {
  KeyframeSchema,
  type Lifecycle,
  projectLifecycle,
  RecordSchema,
  type RunHead,
  RunLiveness,
  type SkeletonResponse,
  type TailResponse,
} from '@vibesys/proto';
import {foldSkeleton, type Skeleton, seedSkeleton} from './model.js';
import {RECORD_HISTORY_LIMIT} from './record-facts.js';

/** A transient gateway observation, fenced by the writer epoch; never part of history. */
export interface RunLivenessHint {
  readonly epoch: number;
  readonly value: RunLiveness;
  readonly head?: RunHead | undefined;
}

/** Records before a keyframe's boundary that the first Tail also delivers, for the run's record log. */
export const RECORD_WINDOW = BigInt(RECORD_HISTORY_LIMIT);

export interface SkeletonLoad {
  readonly skeleton: Skeleton;
  readonly nextSeq: bigint;
  /**
   * Where the first Tail starts: a keyframe hides the records it summarises, so the Tail reaches
   * back a window to show the latest of them. Those records only feed the record log.
   */
  readonly windowFrom: bigint | undefined;
  /**
   * Set when the keyframe lacks failure facts (it predates `failures_tracked`): the records before
   * this sequence were never read, so the error notices among them are paged in from the log.
   */
  readonly failuresBefore: bigint | undefined;
  readonly committedSeq: bigint;
  readonly complete: boolean;
}

/** Where the records a keyframe hides end, less the window the first Tail re-reads. */
function windowStart(response: SkeletonResponse): bigint {
  return response.nextSeq > RECORD_WINDOW ? response.nextSeq - RECORD_WINDOW : 0n;
}

function withObservedHead(skeleton: Skeleton, response: SkeletonResponse): Skeleton {
  if (response.head === undefined) return skeleton;
  if (response.head.committedSeq !== response.committedSeq)
    throw new Error('skeleton.head.committed_seq: differs from committed_seq');
  return {...skeleton, head: response.head};
}

/** Decode the server's bounded committed prefix. No keyframe means replay; next_seq remains authoritative for phase 1 gaps. */
export function ingestSkeleton(response: SkeletonResponse): SkeletonLoad {
  const initial =
    response.keyframe === undefined
      ? seedSkeleton()
      : seedSkeleton(fromBinary(KeyframeSchema, response.keyframe));
  if (initial.throughSeq !== undefined && initial.throughSeq > response.committedSeq)
    throw new Error('keyframe.through_seq: exceeds committed_seq');
  const folded = foldSkeleton(
    initial,
    response.records.map(bytes => fromBinary(RecordSchema, bytes)),
  );
  const skeleton = withObservedHead(folded, response);
  if (skeleton.throughSeq !== undefined && response.nextSeq <= skeleton.throughSeq)
    throw new Error('skeleton.next_seq: does not follow delivered prefix');
  if (
    (skeleton.throughSeq ?? 0n) > response.committedSeq ||
    (response.complete && (skeleton.throughSeq ?? 0n) !== response.committedSeq)
  ) {
    throw new Error('skeleton.complete: inconsistent committed prefix');
  }
  return {
    skeleton,
    nextSeq: response.nextSeq,
    windowFrom:
      response.keyframe === undefined
        ? undefined
        : initial.recordsTracked
          ? windowStart(response)
          : 0n,
    failuresBefore:
      response.keyframe === undefined || skeleton.failuresTracked || !skeleton.recordsTracked
        ? undefined
        : windowStart(response),
    committedSeq: response.committedSeq,
    complete: response.complete,
  };
}

/** Catch-up and live batches share the same data fold and cursor. */
export function ingestTail(state: SkeletonLoad, response: TailResponse): SkeletonLoad {
  const records = response.records.map(bytes => fromBinary(RecordSchema, bytes));
  const first = records[0];
  if (first && first.seq < state.nextSeq)
    throw new Error('tail.records.seq: precedes requested next_seq');
  const folded = foldSkeleton(state.skeleton, records);
  if (response.head !== undefined && response.head.committedSeq !== response.throughSeq)
    throw new Error('tail.head.committed_seq: differs from through_seq');
  const skeleton = response.head === undefined ? folded : {...folded, head: response.head};
  if (response.throughSeq !== (skeleton.throughSeq ?? 0n))
    throw new Error('tail.through_seq: does not match delivered prefix');
  return {
    ...state,
    windowFrom: undefined,
    skeleton,
    nextSeq: skeleton.throughSeq === undefined ? state.nextSeq : skeleton.throughSeq + 1n,
    complete:
      state.complete ||
      (skeleton.throughSeq !== undefined &&
        skeleton.throughSeq >= state.committedSeq &&
        skeleton.throughSeq >= state.nextSeq),
  };
}

/** Parked and every ended outcome close the epoch. Starting, running and draining do not. */
export function epochHasEnded(lifecycle: Lifecycle | undefined): boolean {
  return lifecycle?.state.case === 'parked' || lifecycle?.state.case === 'ended';
}

/** Read current-epoch writer facts with a transient hint; never mutate the recorded lifecycle. */
export function observeRun(skeleton: Skeleton, hint?: RunLivenessHint) {
  const epoch = skeleton.epochs.at(-1);
  const latest = skeleton.lifecycles.at(-1);
  const start = skeleton.epochs.find(item => item.epoch?.epoch === epoch?.epoch?.epoch);
  const recorded = latest && (!start || latest.seq >= start.seq) ? latest.lifecycle : undefined;
  const recordedEpoch = epoch?.epoch?.epoch ?? 0;
  const head = hint?.head;
  const currentHead =
    head?.epoch === hint?.epoch &&
    head !== undefined &&
    head.lifecycle !== undefined &&
    head.epoch >= recordedEpoch &&
    (head.committedSeq ?? -1n) >= (skeleton.throughSeq ?? 0n);
  const observedEpoch = currentHead ? head.epoch : recordedEpoch;
  const lifecycle = projectLifecycle(currentHead ? head.lifecycle : recorded);
  const final = epochHasEnded(lifecycle);
  return {
    epoch: observedEpoch,
    lifecycle,
    final,
    interrupted: !final && hint?.epoch === observedEpoch && hint?.value === RunLiveness.INTERRUPTED,
  };
}

/** A complete final prefix needs no live follower; a prior epoch's end is not final. */
export function isFinalLoad(load: SkeletonLoad | undefined): boolean {
  return load?.complete === true && observeRun(load.skeleton).final;
}

/** Where a load stands; a pure function of the facts the gateway has told the reader. */
export type LoadProgress =
  | {readonly phase: 'skeleton'}
  | {readonly phase: 'history'; readonly through: bigint; readonly committed: bigint}
  | {readonly phase: 'complete'};

export function loadProgress(load: SkeletonLoad | undefined): LoadProgress {
  // Nothing to show is still the first phase, however complete the empty prefix is.
  if (load === undefined || load.skeleton.epochs.length === 0) return {phase: 'skeleton'};
  if (load.complete) return {phase: 'complete'};
  return {phase: 'history', through: load.skeleton.throughSeq ?? 0n, committed: load.committedSeq};
}

/** The share of the committed history applied, from 0 to 1. */
export function progressFraction(progress: LoadProgress): number {
  switch (progress.phase) {
    case 'skeleton':
      return 0;
    case 'complete':
      return 1;
    case 'history':
      return progress.committed <= 0n
        ? 1
        : Math.min(1, Number((progress.through * 1000n) / progress.committed) / 1000);
  }
}
