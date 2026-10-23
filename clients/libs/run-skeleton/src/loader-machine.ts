import {unprovided} from '@vibesys/machine';
import type {SkeletonResponse, TailResponse} from '@vibesys/proto';
import {assign, setup} from 'xstate';
import {ingestSkeleton, ingestTail, isFinalLoad, type SkeletonLoad} from './loading.js';
import {type LoadRefusal, refusalOf} from './status.js';

export interface SkeletonLoaderInput {
  readonly runId: string;
  readonly timeoutMs: number;
}
export interface TailInput {
  readonly runId: string;
  /** Where the request starts; records before `cursor` feed the record log and nothing else. */
  readonly fromSeq: bigint;
  readonly cursor: bigint;
  readonly receive: (event: LoaderEvent) => void;
}
export type LoaderEvent =
  | {type: 'tail'; response: TailResponse}
  | {type: 'failed'; error: unknown}
  | {type: 'closed'};
export type SkeletonLoadFailure =
  | {readonly kind: 'deadline'; readonly operation: 'skeleton' | 'catch-up'}
  | {readonly kind: 'transport'; readonly cause: unknown}
  | {readonly kind: 'refused'; readonly reason: LoadRefusal; readonly cause: unknown}
  | {readonly kind: 'data'; readonly cause: unknown}
  | {readonly kind: 'stream-ended'};

interface Context extends SkeletonLoaderInput {
  load: SkeletonLoad | undefined;
  error: SkeletonLoadFailure | undefined;
  /** Failures since the history last advanced; backoff grows with it and it is bounded. */
  attempts: number;
}

const RETRY_BASE_MS = 1000;
export const RETRY_CAP_MS = 30_000;
/** Six tries span about a minute of backoff; the person then decides to retry. */
export const MAX_ATTEMPTS = 6;

export function retryDelay(attempts: number): number {
  return Math.min(RETRY_BASE_MS * 2 ** Math.max(0, attempts - 1), RETRY_CAP_MS);
}

/** Pure control state. The shell supplies unary and stream actors; exiting cancels their work. */
export const skeletonLoaderMachine = setup({
  types: {context: {} as Context, input: {} as SkeletonLoaderInput, events: {} as LoaderEvent},
  actors: {
    skeleton: unprovided<SkeletonResponse, string>('skeleton'),
    tail: unprovided<void, TailInput>('tail'),
  },
  delays: {
    deadline: ({context}) => context.timeoutMs,
    retry: ({context}) => retryDelay(context.attempts),
  },
  actions: {
    recordRefusal: assign(({event}) => ({error: refusal(errorOf(event))})),
    recordFailure: assign(({context, event}) => ({
      error: tailFailure(context, transportFailure(errorOf(event))),
    })),
    recordStreamEnded: assign(({context}) => ({
      error: tailFailure(context, {kind: 'stream-ended'}),
    })),
    clearError: assign({error: undefined}),
  },
  guards: {
    hasError: ({context}) => context.error !== undefined,
    caughtUp: ({context}) => context.load?.complete === true,
    // A complete terminal prefix loses nothing by waiting: a relaunch may still extend it.
    exhausted: ({context}) => context.attempts >= MAX_ATTEMPTS && !isFinalLoad(context.load),
    hasLoad: ({context}) => context.load !== undefined,
    refused: ({event}) => refusalOf(errorOf(event)) !== undefined,
  },
}).createMachine({
  id: 'skeletonLoader',
  context: ({input}) => ({...input, load: undefined, error: undefined, attempts: 0}),
  initial: 'loading',
  states: {
    loading: {
      invoke: {
        id: 'skeleton',
        src: 'skeleton',
        input: ({context}) => context.runId,
        onDone: {
          target: 'loaded',
          actions: assign(({event}) => safely(() => ingestSkeleton(event.output))),
        },
        onError: [
          {guard: 'refused', target: 'failed', actions: 'recordRefusal'},
          {target: 'recovering', actions: 'recordFailure'},
        ],
      },
      after: {
        deadline: {
          target: 'recovering',
          actions: assign({
            error: ({context}) => tailFailure(context, {kind: 'deadline', operation: 'skeleton'}),
          }),
        },
      },
    },
    loaded: {always: [{guard: 'hasError', target: 'failed'}, {target: 'following'}]},
    following: {
      always: {guard: 'hasError', target: 'failed'},
      initial: 'catchingUp',
      invoke: {
        id: 'tail',
        src: 'tail',
        input: ({context, self}) => {
          if (!context.load) throw new Error('Tail requires loaded Skeleton');
          return {
            runId: context.runId,
            fromSeq: context.load.windowFrom ?? context.load.nextSeq,
            cursor: context.load.nextSeq,
            receive: event => self.send(event),
          };
        },
        onDone: {target: 'recovering', actions: 'recordStreamEnded'},
        onError: [
          {guard: 'refused', target: 'failed', actions: 'recordRefusal'},
          {target: 'recovering', actions: 'recordFailure'},
        ],
      },
      on: {
        tail: {
          actions: assign(({context, event}) => ({
            ...safely(() => {
              if (!context.load) throw new Error('Tail without Skeleton');
              return ingestTail(context.load, event.response);
            }),
            attempts: 0,
          })),
        },
        failed: [
          {guard: 'refused', target: 'failed', actions: 'recordRefusal'},
          {target: 'recovering', actions: 'recordFailure'},
        ],
        closed: {target: 'recovering', actions: 'recordStreamEnded'},
      },
      states: {
        catchingUp: {
          always: {guard: 'caughtUp', target: 'live'},
          after: {
            deadline: {
              target: '#skeletonLoader.recovering',
              actions: assign({
                error: (): SkeletonLoadFailure => ({kind: 'deadline', operation: 'catch-up'}),
              }),
            },
          },
        },
        live: {},
      },
    },
    /** A cut stream resumes from the cursor; only a run with no prefix asks for a Skeleton. */
    recovering: {
      entry: assign({attempts: ({context}) => context.attempts + 1}),
      always: {guard: 'exhausted', target: 'failed'},
      after: {
        retry: [
          {guard: 'hasLoad', target: 'following', actions: 'clearError'},
          {target: 'loading'},
        ],
      },
    },
    failed: {type: 'final'},
  },
});

function safely(fold: () => SkeletonLoad): Partial<Context> {
  try {
    return {load: fold(), error: undefined};
  } catch (error) {
    return {error: {kind: 'data', cause: error}};
  }
}

function errorOf(event: {type: string}): unknown {
  return 'error' in event ? event.error : undefined;
}

function refusal(cause: unknown): SkeletonLoadFailure {
  const reason = refusalOf(cause);
  return reason === undefined ? transportFailure(cause) : {kind: 'refused', reason, cause};
}

function transportFailure(cause: unknown): SkeletonLoadFailure {
  return {kind: 'transport', cause};
}

/** Losing a follower after a complete terminal prefix loses no live observation. */
function tailFailure(
  context: Context,
  failure: SkeletonLoadFailure,
): SkeletonLoadFailure | undefined {
  return isFinalLoad(context.load) ? undefined : failure;
}
