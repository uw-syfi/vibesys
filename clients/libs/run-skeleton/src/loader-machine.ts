import {unprovided} from '@vibesys/machine';
import type {SkeletonResponse, Record as StoreRecord, TailResponse} from '@vibesys/proto';
import {assign, setup} from 'xstate';
import {ingestSkeleton, ingestTail, isFinalLoad, type SkeletonLoad} from './loading.js';
import {absorbFailures, absorbRecordFacts} from './model.js';
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
export interface FailuresInput {
  readonly runId: string;
  /** The records from here on were read; the error notices before it are still to be found. */
  readonly before: bigint | undefined;
  /** Where the current epoch's records begin, and its number: nothing older can be its banner. */
  readonly epochStart: bigint;
  readonly epoch: number;
  readonly receive: (event: LoaderEvent) => void;
}
export type LoaderEvent =
  | {type: 'history'; records: readonly StoreRecord[]; through: bigint}
  | {type: 'failures'; records: readonly StoreRecord[]}
  | {type: 'failures-incomplete'}
  | {type: 'tail'; response: TailResponse}
  /** A Tail message that only fed the record log reached `through`: a resume starts after it. */
  | {type: 'window'; through: bigint}
  | {type: 'failed'; error: unknown}
  | {type: 'closed'};
export type SkeletonLoadFailure =
  | {readonly kind: 'deadline'; readonly operation: 'skeleton' | 'catch-up'}
  | {readonly kind: 'transport'; readonly cause: unknown}
  | {readonly kind: 'refused'; readonly reason: LoadRefusal; readonly cause: unknown}
  | {readonly kind: 'data'; readonly cause: unknown}
  | {readonly kind: 'stream-ended'}
  /** The whole committed prefix is empty and nothing arrived within the deadline. */
  | {readonly kind: 'empty'};

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
    failures: unprovided<void, FailuresInput>('failures'),
  },
  delays: {
    deadline: ({context}) => context.timeoutMs,
    retry: ({context}) => retryDelay(context.attempts),
  },
  actions: {
    publishSkeleton: (
      _context,
      _params: {response: SkeletonResponse; load: SkeletonLoad | undefined; valid: boolean},
    ) => undefined,
    recordRefusal: assign(({event}) => ({error: refusal(errorOf(event))})),
    recordFailure: assign(({context, event}) => ({
      error: tailFailure(context, transportFailure(errorOf(event))),
    })),
    recordStreamEnded: assign(({context}) => ({
      error: tailFailure(context, {kind: 'stream-ended'}),
    })),
    clearError: assign({error: undefined}),
    // Failure facts are best effort: the screen stands without them.
    ignore: () => undefined,
  },
  guards: {
    hasError: ({context}) => context.error !== undefined,
    caughtUp: ({context}) => context.load?.complete === true,
    // A complete terminal prefix loses nothing by waiting: a relaunch may still extend it.
    exhausted: ({context}) => context.attempts >= MAX_ATTEMPTS && !isFinalLoad(context.load),
    hasLoad: ({context}) => context.load !== undefined,
    // Complete is not content: a run directory whose log is not created yet has an empty prefix.
    unpainted: ({context}) => (context.load?.skeleton.epochs.length ?? 0) === 0,
    painted: ({context}) => (context.load?.skeleton.epochs.length ?? 0) > 0,
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
          actions: [
            assign(({event}) => safely(() => ingestSkeleton(event.output))),
            {
              type: 'publishSkeleton',
              params: ({context, event}) => ({
                response: event.output,
                load: context.load,
                valid: context.error === undefined,
              }),
            },
          ],
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
        history: {
          actions: assign(({context, event}) =>
            context.load === undefined
              ? {}
              : {
                  load: {
                    ...context.load,
                    skeleton: absorbRecordFacts(
                      context.load.skeleton,
                      event.records,
                      event.through,
                    ),
                  },
                },
          ),
        },
        failures: {
          actions: assign(({context, event}) =>
            context.load === undefined
              ? {}
              : {
                  load: {
                    ...context.load,
                    skeleton: absorbFailures(context.load.skeleton, event.records),
                  },
                },
          ),
        },
        'failures-incomplete': {
          actions: assign(({context}) =>
            context.load === undefined
              ? {}
              : {load: {...context.load, diagnosticsIncomplete: true}},
          ),
        },
        window: {
          actions: assign(({context, event}) =>
            context.load === undefined
              ? {}
              : {
                  load: {
                    ...context.load,
                    windowFrom:
                      event.through + 1n < context.load.nextSeq ? event.through + 1n : undefined,
                  },
                },
          ),
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
        /** Caught up. With nothing to show yet, the wait for the first record is bounded. */
        live: {
          // Once caught up, page in what an old keyframe never said of failures; it is done once.
          invoke: {
            id: 'failures',
            src: 'failures',
            input: ({context, self}) => {
              const skeleton = context.load?.skeleton;
              const epoch = skeleton?.epochs.at(-1)?.epoch?.epoch ?? 0;
              return {
                runId: context.runId,
                before: context.load?.failuresBefore,
                epoch,
                epochStart: skeleton?.epochs.find(item => item.epoch?.epoch === epoch)?.seq ?? 0n,
                receive: event => self.send(event),
              };
            },
            onDone: {
              actions: assign(({context}) =>
                context.load === undefined
                  ? {}
                  : {load: {...context.load, failuresBefore: undefined}},
              ),
            },
            onError: {actions: 'ignore'},
          },
          initial: 'routing',
          states: {
            routing: {always: [{guard: 'unpainted', target: 'waiting'}, {target: 'populated'}]},
            waiting: {
              always: {guard: 'painted', target: 'populated'},
              after: {
                deadline: {
                  target: '#skeletonLoader.failed',
                  actions: assign({error: (): SkeletonLoadFailure => ({kind: 'empty'})}),
                },
              },
            },
            populated: {},
          },
        },
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
