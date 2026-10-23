/** What the load shows: a pure reading of the reader's facts, in status words and numbers. */
import type {LoadRefusal, SkeletonLoad, SkeletonLoadFailure} from '@vibesys/run-skeleton';
import {progressFraction, type Skeleton} from '@vibesys/run-skeleton';
import type {GatewayRunContext} from './gateway-machine.js';

export type LoadView =
  | {readonly kind: 'ready'}
  | {
      readonly kind: 'loading';
      readonly label: 'Skeleton' | 'History';
      readonly fraction: number;
      readonly detail: string;
    }
  | {readonly kind: 'reconnecting'}
  | {readonly kind: 'failed'; readonly word: string};

const REFUSAL_WORDS: Readonly<Record<LoadRefusal, string>> = {
  'not-found': 'No run log',
  'data-loss': 'Data loss',
  'too-large': 'Too large',
  unsupported: 'Unsupported',
  invalid: 'Invalid',
  denied: 'Denied',
};

function failureWord(failure: SkeletonLoadFailure): string {
  switch (failure.kind) {
    case 'refused':
      return REFUSAL_WORDS[failure.reason];
    case 'data':
      return 'Invalid records';
    case 'deadline':
      return 'Timed out';
    case 'empty':
      return 'No records';
    case 'transport':
    case 'stream-ended':
      return 'Unreachable';
  }
}

export function loadView(
  context: Pick<GatewayRunContext, 'progress' | 'failure' | 'terminal'>,
): LoadView {
  const {progress, failure, terminal} = context;
  if (failure !== undefined)
    return terminal ? {kind: 'failed', word: failureWord(failure)} : {kind: 'reconnecting'};
  switch (progress.phase) {
    case 'complete':
      return {kind: 'ready'};
    case 'skeleton':
      return {kind: 'loading', label: 'Skeleton', fraction: 0, detail: ''};
    case 'history':
      return {
        kind: 'loading',
        label: 'History',
        fraction: progressFraction(progress),
        detail: `${progress.through} of ${progress.committed}`,
      };
  }
}

/** A prefix is progress, not settled run facts. Publish the page after committed history catches up. */
export function paintedSkeleton(load: SkeletonLoad | undefined): Skeleton | undefined {
  return load?.complete === true && load.skeleton.epochs.length > 0 ? load.skeleton : undefined;
}
