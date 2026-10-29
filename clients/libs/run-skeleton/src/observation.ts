/** The run reader owns independent history and transient host observations. */
import {unprovided} from '@vibesys/machine';
import {RunLiveness} from '@vibesys/proto';
import {assign, fromCallback, setup} from 'xstate';
import type {SkeletonLoadFailure} from './loader-machine.js';
import {retryDelay} from './loader-machine.js';
import {epochHasEnded, type RunLivenessHint, type SkeletonLoad} from './loading.js';

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
/** The epochs this observation has seen closed (no writer) and alive (a writer holding the lock). */
interface WriterEpochs {
  readonly closed: number;
  readonly alive: number;
}
const NO_EPOCHS: WriterEpochs = {closed: 0, alive: 0};

/**
 * A lock probe is a fact of the writer that held the lock. A free lock at a successor epoch that
 * this observation has not seen alive, after a closed predecessor, is the gap before its writer
 * starts: unknown, never interrupted. A fresh observation (the page re-reads when facts stay
 * unresolved) has no predecessor and takes the probe at its word.
 */
function fenceProbe(
  seen: WriterEpochs,
  hint: RunLivenessHint | undefined,
): {readonly hint: RunLivenessHint | undefined; readonly seen: WriterEpochs} {
  if (!hint) return {hint, seen};
  const pending =
    hint.value === RunLiveness.INTERRUPTED &&
    seen.closed > 0 &&
    seen.closed < hint.epoch &&
    seen.alive < hint.epoch;
  const value = pending ? RunLiveness.UNSPECIFIED : hint.value;
  const closed =
    value === RunLiveness.ENDED ||
    value === RunLiveness.PARKED ||
    value === RunLiveness.INTERRUPTED ||
    epochHasEnded(hint.head?.lifecycle);
  return {
    hint: pending ? {...hint, value} : hint,
    seen: {
      closed: closed ? Math.max(seen.closed, hint.epoch) : seen.closed,
      alive: value === RunLiveness.ALIVE ? Math.max(seen.alive, hint.epoch) : seen.alive,
    },
  };
}

interface Context extends ObservationInput {
  seen: WriterEpochs;
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
    seen: NO_EPOCHS,
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
    hint: {
      actions: assign(({context, event}) => {
        const fenced = fenceProbe(context.seen, event.hint);
        return {hint: fenced.hint, seen: fenced.seen};
      }),
    },
  },
  initial: 'watching',
  states: {
    // The host refused the run: there is nothing to watch, and a new observation is the only retry.
    stopped: {},
    watching: {
      always: {guard: ({context}) => context.failure?.kind === 'refused', target: 'stopped'},
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
        live: {
          after: {deadline: '#runObservation.retrying'},
          on: {
            ready: {
              target: 'live',
              reenter: true,
              actions: assign({watchFailed: false, watchAttempts: 0}),
            },
          },
        },
      },
    },
    retrying: {
      always: {guard: ({context}) => context.failure?.kind === 'refused', target: 'stopped'},
      entry: assign({
        watchFailed: true,
        hint: undefined,
        watchAttempts: ({context}) => context.watchAttempts + 1,
      }),
      after: {retry: 'watching'},
    },
  },
});
