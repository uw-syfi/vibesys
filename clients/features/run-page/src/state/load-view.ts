/** What the load shows: a pure reading of the reader's facts, in status words and numbers. */
import type {LoadRefusal, SkeletonLoad, SkeletonLoadFailure} from '@vibesys/run-skeleton';
import {progressFraction, type Skeleton} from '@vibesys/run-skeleton';
import type {GatewayRunContext} from './gateway-machine.js';
import type {RunPageState} from './run-page.js';

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

/** Any valid epoch prefix can paint the page; totals carry their own catch-up state. */
export function paintedSkeleton(load: SkeletonLoad | undefined): Skeleton | undefined {
  return load && load.skeleton.epochs.length > 0 ? load.skeleton : undefined;
}

/** What the loader's batches change in the context (and in `ui`, below); any other change is for the page at once. */
const BATCH_FIELDS: ReadonlySet<string> = new Set<keyof GatewayRunContext>([
  'load',
  'progress',
  'replies',
  'ui',
]);
/** The record log grows with each batch; the rest of the screen state is the person's. */
const BATCH_UI_FIELDS: ReadonlySet<string> = new Set<keyof RunPageState>([
  'recordLog',
  'recordLogError',
]);

function sameOutside<T extends object>(batch: ReadonlySet<string>, previous: T, next: T): boolean {
  return [...new Set([...Object.keys(previous), ...Object.keys(next)])].every(
    key => batch.has(key) || Object.is(Reflect.get(previous, key), Reflect.get(next, key)),
  );
}

/**
 * The history is still being read and `next` differs from `previous` only by what the batches
 * bring, so the page may draw a little behind it (`projectionPace`). A liveness hint, a failure,
 * the person's click or content that arrived come with their own change and are never held.
 */
export function isCatchUpBatch(
  previous: GatewayRunContext | undefined,
  next: GatewayRunContext,
): boolean {
  if (next.progress.phase !== 'history' || next.failure !== undefined) return false;
  if (previous === undefined) return true;
  return (
    sameOutside(BATCH_FIELDS, previous, next) && sameOutside(BATCH_UI_FIELDS, previous.ui, next.ui)
  );
}
