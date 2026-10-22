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

export interface SkeletonLoad {
  readonly skeleton: Skeleton;
  readonly nextSeq: bigint;
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
