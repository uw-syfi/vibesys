/** The run reader owns independent history and transient host observations. */
import {RunLiveness} from '@vibesys/proto';
import {assign, fromCallback, setup} from 'xstate';
import type {SkeletonLoadFailure} from './loader-machine.js';
import {epochHasEnded, type RunLivenessHint, type SkeletonLoad} from './loading.js';

export interface ObservationInput {
  readonly runId: string;
  /** The history loader's own deadline for a reply. The host's liveness has no timer here. */
  readonly timeoutMs: number;
}

/**
 * What the host says of one run. The host's subscription owns the stream, its lease and its
 * retries, so the reader holds none: it is told whether the host is delivering.
 */
export type RunLivenessState =
  /** Nothing yet, and nothing lost. */
  | {readonly kind: 'waiting'}
  /** The host is delivering; `hint` is the run's liveness, absent when the host lists no such run. */
  | {readonly kind: 'live'; readonly hint: RunLivenessHint | undefined}
  /** The host stopped delivering: its last word is no longer a fact. */
  | {readonly kind: 'lost'};

/** One run's liveness as an external store, which the reader subscribes to while it watches. */
export interface RunLivenessFeed {
  subscribe(listener: () => void): () => void;
  /** The same object until the state changes. */
  getSnapshot(): RunLivenessState;
}

export type ObservationEvent =
  | {
      type: 'read';
      load: SkeletonLoad | undefined;
      failure: SkeletonLoadFailure | undefined;
      /** The loader gave up: only a new observation can load this run. */
      terminal: boolean;
    }
  | {type: 'liveness'; state: RunLivenessState};
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
  /** The host delivered and has stopped: no screen may take its last word for a fact. */
  watchFailed: boolean;
}
export const runObservationMachine = setup({
  types: {context: {} as Context, input: {} as ObservationInput, events: {} as ObservationEvent},
  actors: {
    history: fromCallback<ObservationEvent, ObservationOperation>(() => {
      throw new Error('history not provided');
    }),
    watch: fromCallback<ObservationEvent, ObservationOperation>(() => {
      throw new Error('run liveness not provided');
    }),
  },
  actions: {
    // A probe is a fact of the writer that held the lock, and only the host delivering gives one.
    heard: assign(({context, event}) => {
      if (event.type !== 'liveness' || event.state.kind !== 'live') return {};
      const fenced = fenceProbe(context.seen, event.state.hint);
      return {hint: fenced.hint, seen: fenced.seen, watchFailed: false};
    }),
    lost: assign({watchFailed: true, hint: undefined}),
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
      },
      on: {
        liveness: [
          {guard: ({event}) => event.state.kind === 'live', target: '.live', actions: 'heard'},
          {guard: ({event}) => event.state.kind === 'lost', target: '.waiting', actions: 'lost'},
          {target: '.waiting'},
        ],
      },
      states: {
        waiting: {},
        live: {},
      },
    },
  },
});
