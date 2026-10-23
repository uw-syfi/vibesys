import {fromBinary} from '@bufbuild/protobuf';
import {
  KeyframeSchema,
  type Lifecycle,
  RecordSchema,
  type RunHead,
  RunLiveness,
  type SkeletonResponse,
  type TailResponse,
} from '@vibesys/proto';
import {foldSkeleton, type Skeleton, seedSkeleton} from './model.js';

/** A transient gateway observation, fenced by the writer epoch; never part of history. */
export interface RunLivenessHint {
  readonly epoch: number;
  readonly value: RunLiveness;
  readonly head?: RunHead | undefined;
}

/** Records before a keyframe's boundary that the first Tail also delivers, for the run's record log. */
export const RECORD_WINDOW = 256n;

export interface SkeletonLoad {
  readonly skeleton: Skeleton;
  readonly nextSeq: bigint;
  /**
   * Where the first Tail starts: a keyframe hides the records it summarises, so the Tail reaches
   * back a window to show the latest of them. Those records only feed the record log.
   */
  readonly windowFrom: bigint | undefined;
  readonly committedSeq: bigint;
  readonly complete: boolean;
}

/** Decode the server's bounded committed prefix. No keyframe means replay; next_seq remains authoritative for phase 1 gaps. */
export function ingestSkeleton(response: SkeletonResponse): SkeletonLoad {
  const initial =
    response.keyframe === undefined
      ? seedSkeleton()
      : seedSkeleton(fromBinary(KeyframeSchema, response.keyframe));
  if (initial.throughSeq !== undefined && initial.throughSeq > response.committedSeq)
    throw new Error('keyframe.through_seq: exceeds committed_seq');
  const skeleton = foldSkeleton(
    initial,
    response.records.map(bytes => fromBinary(RecordSchema, bytes)),
  );
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
        : response.nextSeq > RECORD_WINDOW
          ? response.nextSeq - RECORD_WINDOW
          : 0n,
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
  const skeleton = foldSkeleton(state.skeleton, records);
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
  const lifecycle = latest && (!start || latest.seq >= start.seq) ? latest.lifecycle : undefined;
  const final = epochHasEnded(lifecycle);
  return {
    epoch: epoch?.epoch?.epoch ?? 0,
    lifecycle,
    final,
    interrupted:
      !final && hint?.epoch === epoch?.epoch?.epoch && hint?.value === RunLiveness.INTERRUPTED,
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
  if (load === undefined) return {phase: 'skeleton'};
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
