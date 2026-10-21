import {unprovided} from '@vibesys/machine';
import {
  copyRelaunch,
  copyRelaunchObservation,
  type RelaunchLiveness,
  type RelaunchObservation,
  type RelaunchRequest,
  type RelaunchResponse,
  RunLiveness,
  relaunchTarget,
} from '@vibesys/runs/intents';
import {assign, fromCallback, setup} from 'xstate';

export type RelaunchEvent =
  | {readonly type: 'relaunch'}
  | {readonly type: 'observe'; readonly observation: RelaunchObservation}
  | {readonly type: 'liveness'; readonly value: RelaunchLiveness};

/** An uncertain launch is never retried automatically; observation stays independent of I/O. */
export const relaunchMachine = setup({
  types: {
    context: {} as {
      readonly request: RelaunchRequest;
      readonly observation: RelaunchObservation;
      readonly liveness: RelaunchLiveness;
      readonly result: RelaunchResponse | null;
    },
    input: {} as {readonly request: RelaunchRequest; readonly observation: RelaunchObservation},
    events: {} as RelaunchEvent,
  },
  actors: {
    launch: unprovided<RelaunchResponse, RelaunchRequest>('launch'),
    liveness: fromCallback<RelaunchEvent>(() => {
      throw new Error('liveness was not provided');
    }),
  },
  guards: {
    eligible: ({context}) => relaunchTarget(context.observation, context.liveness) !== null,
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
  }),
  invoke: {
    id: 'liveness',
    src: 'liveness',
    onError: {actions: assign({liveness: {epoch: 0n, value: RunLiveness.UNSPECIFIED}})},
  },
  on: {
    observe: {
      guard: ({context, event}) => event.observation.epoch >= context.observation.epoch,
      actions: assign(({event}) => ({observation: copyRelaunchObservation(event.observation)})),
    },
    liveness: {actions: assign(({event}) => ({liveness: event.value}))},
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
          })),
        },
      },
    },
    launching: {
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
    uncertain: {always: {guard: 'nextEpoch', target: 'ready'}},
  },
});
