import {unprovided} from '@vibesys/machine';
import {
  confirmedRelaunchEpoch,
  copyRelaunch,
  copyRelaunchObservation,
  type RelaunchLiveness,
  type RelaunchObservation,
  type RelaunchRequest,
  type RelaunchResponse,
  RunLiveness,
  type RunWriterFacts,
  relaunchTarget,
  sameRelaunchObservation,
} from '@vibesys/runs/intents';
import {assign, setup} from 'xstate';

export type RelaunchEvent =
  | {readonly type: 'relaunch'}
  | {readonly type: 'epoch'; readonly epoch: bigint}
  | {readonly type: 'writer'; readonly facts: RunWriterFacts};

/** An uncertain launch is never retried automatically; observation stays independent of I/O. */
export const relaunchMachine = setup({
  types: {
    context: {} as {
      readonly request: RelaunchRequest;
      readonly observation: RelaunchObservation;
      readonly liveness: RelaunchLiveness;
      readonly result: RelaunchResponse | null;
      readonly historyEpoch: bigint;
      readonly attempted: boolean;
    },
    input: {} as {readonly request: RelaunchRequest; readonly observation: RelaunchObservation},
    events: {} as RelaunchEvent,
  },
  actors: {
    launch: unprovided<RelaunchResponse, RelaunchRequest>('launch'),
  },
  guards: {
    eligible: ({context}) => relaunchTarget(context.observation, context.liveness) !== null,
    confirmed: ({context}) =>
      context.attempted &&
      confirmedRelaunchEpoch(
        context.request,
        context.observation,
        context.liveness,
        context.historyEpoch,
      ) !== null,
    nextEpoch: ({context}) =>
      context.observation.epoch > context.request.expectedEpoch &&
      relaunchTarget(context.observation, context.liveness) !== null,
  },
  delays: {deadline: 35_000},
}).createMachine({
  id: 'relaunch',
  context: ({input}) => ({
    request: copyRelaunch(input.request),
    observation: copyRelaunchObservation(input.observation),
    liveness: {epoch: 0n, value: RunLiveness.UNSPECIFIED},
    result: null,
    historyEpoch: 0n,
    attempted: false,
  }),
  on: {
    epoch: {
      actions: assign({
        historyEpoch: ({context, event}) =>
          event.epoch > context.historyEpoch ? event.epoch : context.historyEpoch,
      }),
    },
    writer: {
      actions: assign(({context, event}) => ({
        liveness: event.facts.liveness,
        ...(event.facts.observation &&
        event.facts.observation.epoch >= context.observation.epoch &&
        !sameRelaunchObservation(context.observation, event.facts.observation)
          ? {observation: copyRelaunchObservation(event.facts.observation)}
          : {}),
      })),
    },
  },
  initial: 'ready',
  states: {
    ready: {
      on: {
        relaunch: {
          guard: 'eligible',
          target: 'launching',
          actions: assign(({context}) => ({
            request: copyRelaunch({...context.request, expectedEpoch: context.observation.epoch}),
            result: null,
            attempted: true,
          })),
        },
      },
    },
    launching: {
      always: {guard: 'confirmed', target: 'settled'},
      invoke: {
        id: 'launch',
        src: 'launch',
        input: ({context}) => context.request,
        onDone: {target: 'settled', actions: assign(({event}) => ({result: event.output}))},
        onError: {target: 'uncertain'},
      },
      after: {deadline: 'uncertain'},
    },
    settled: {always: {guard: 'nextEpoch', target: 'ready'}},
    uncertain: {always: {guard: 'confirmed', target: 'settled'}},
  },
});

/** Unresolved facts retain one re-read deadline across unknown/catching-up transitions. */
export const writerRecoveryMachine = setup({
  types: {events: {} as {type: 'observe'; waiting: boolean}},
  actions: {
    refresh: () => {
      throw new Error('writer refresh was not provided');
    },
  },
  delays: {deadline: 30_000},
}).createMachine({
  initial: 'current',
  on: {observe: {guard: ({event}) => !event.waiting, target: '.current'}},
  states: {
    current: {on: {observe: {guard: ({event}) => event.waiting, target: 'waiting'}}},
    waiting: {after: {deadline: 'refreshing'}},
    refreshing: {
      entry: 'refresh',
      always: 'waiting',
    },
  },
});
