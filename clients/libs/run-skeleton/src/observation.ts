/** The run reader owns independent history and transient host observations. */
import {unprovided} from '@vibesys/machine';
import {assign, fromCallback, setup} from 'xstate';
import type {SkeletonLoadFailure} from './loader-machine.js';
import {retryDelay} from './loader-machine.js';
import type {RunLivenessHint, SkeletonLoad} from './loading.js';

export interface ObservationInput {
  readonly runId: string;
  readonly timeoutMs: number;
}
export type ObservationEvent =
  | {
      type: 'read';
      load: SkeletonLoad | undefined;
      failure: SkeletonLoadFailure | undefined;
      /** The loader gave up: only a new observation can load this run. */
      terminal: boolean;
    }
  | {type: 'hint'; hint: RunLivenessHint | undefined}
  | {type: 'ready'};
export interface ObservationOperation extends ObservationInput {
  readonly receive: (event: ObservationEvent) => void;
}
interface Context extends ObservationInput {
  load: SkeletonLoad | undefined;
  hint: RunLivenessHint | undefined;
  failure: SkeletonLoadFailure | undefined;
  terminal: boolean;
  watchFailed: boolean;
  /** Consecutive liveness-stream failures; backoff grows with it until the stream is live. */
  watchAttempts: number;
}
export const runObservationMachine = setup({
  types: {context: {} as Context, input: {} as ObservationInput, events: {} as ObservationEvent},
  actors: {
    history: fromCallback<ObservationEvent, ObservationOperation>(() => {
      throw new Error('history not provided');
    }),
    watch: unprovided<void, ObservationOperation>('run liveness'),
  },
  delays: {
    deadline: ({context}) => context.timeoutMs,
    retry: ({context}) => retryDelay(context.watchAttempts),
  },
}).createMachine({
  id: 'runObservation',
  context: ({input}) => ({
    ...input,
    load: undefined,
    hint: undefined,
    failure: undefined,
    terminal: false,
    watchFailed: false,
    watchAttempts: 0,
  }),
  invoke: {
    id: 'history',
    src: 'history',
    input: ({context, self}) => ({...context, receive: event => self.send(event)}),
    onError: {
      actions: assign({failure: ({event}) => ({kind: 'transport', cause: event.error})}),
    },
  },
  on: {
    read: {
      actions: assign({
        load: ({event}) => event.load,
        failure: ({event}) => event.failure,
        terminal: ({event}) => event.terminal,
      }),
    },
    hint: {actions: assign({hint: ({event}) => event.hint})},
  },
  initial: 'watching',
  states: {
    watching: {
      initial: 'waiting',
      invoke: {
        id: 'watch',
        src: 'watch',
        input: ({context, self}) => ({...context, receive: event => self.send(event)}),
        onDone: 'retrying',
        onError: 'retrying',
      },
      states: {
        waiting: {
          after: {deadline: '#runObservation.retrying'},
          on: {ready: {target: 'live', actions: assign({watchFailed: false, watchAttempts: 0})}},
        },
        live: {},
      },
    },
    retrying: {
      entry: assign({watchFailed: true, watchAttempts: ({context}) => context.watchAttempts + 1}),
      after: {retry: 'watching'},
    },
  },
});
