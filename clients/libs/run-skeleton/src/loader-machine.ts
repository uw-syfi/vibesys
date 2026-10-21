import {unprovided} from '@vibesys/machine';
import type {SkeletonResponse, TailResponse} from '@vibesys/proto';
import {assign, setup} from 'xstate';
import {ingestSkeleton, ingestTail, type SkeletonLoad} from './loading.js';

export interface SkeletonLoaderInput {
  readonly runId: string;
  readonly timeoutMs: number;
}
export interface TailInput {
  readonly runId: string;
  readonly fromSeq: bigint;
  readonly receive: (event: LoaderEvent) => void;
}
export type LoaderEvent =
  | {type: 'tail'; response: TailResponse}
  | {type: 'failed'; error: unknown}
  | {type: 'closed'};
export type SkeletonLoadFailure =
  | {readonly kind: 'deadline'; readonly operation: 'skeleton' | 'catch-up'}
  | {readonly kind: 'transport'; readonly cause: unknown}
  | {readonly kind: 'data'; readonly cause: unknown}
  | {readonly kind: 'stream-ended'};

interface Context extends SkeletonLoaderInput {
  load: SkeletonLoad | undefined;
  error: SkeletonLoadFailure | undefined;
}

/** Pure control state. The shell supplies unary and stream actors; exiting cancels their work. */
export const skeletonLoaderMachine = setup({
  types: {context: {} as Context, input: {} as SkeletonLoaderInput, events: {} as LoaderEvent},
  actors: {
    skeleton: unprovided<SkeletonResponse, string>('skeleton'),
    tail: unprovided<void, TailInput>('tail'),
  },
  delays: {deadline: ({context}) => context.timeoutMs, retry: 1000},
  guards: {
    hasError: ({context}) => context.error !== undefined,
    caughtUp: ({context}) => context.load?.complete === true,
  },
}).createMachine({
  id: 'skeletonLoader',
  context: ({input}) => ({...input, load: undefined, error: undefined}),
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
        onError: {
          target: 'recovering',
          actions: assign({error: ({event}) => transportFailure(event.error)}),
        },
      },
      after: {
        deadline: {
          target: 'recovering',
          actions: assign({
            error: (): SkeletonLoadFailure => ({kind: 'deadline', operation: 'skeleton'}),
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
            fromSeq: context.load.nextSeq,
            receive: event => self.send(event),
          };
        },
        onDone: {
          target: 'recovering',
          actions: assign({error: (): SkeletonLoadFailure => ({kind: 'stream-ended'})}),
        },
        onError: {
          target: 'recovering',
          actions: assign({error: ({event}) => transportFailure(event.error)}),
        },
      },
      on: {
        tail: {
          actions: assign(({context, event}) =>
            safely(() => {
              if (!context.load) throw new Error('Tail without Skeleton');
              return ingestTail(context.load, event.response);
            }),
          ),
        },
        failed: {
          target: 'recovering',
          actions: assign({error: ({event}) => transportFailure(event.error)}),
        },
        closed: {
          target: 'recovering',
          actions: assign({error: (): SkeletonLoadFailure => ({kind: 'stream-ended'})}),
        },
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
    recovering: {after: {retry: 'loading'}},
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

function transportFailure(cause: unknown): SkeletonLoadFailure {
  return {kind: 'transport', cause};
}
