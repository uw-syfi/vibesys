import {unprovided} from '@vibesys/machine';
import {copyRelaunch, type RelaunchRequest, type RelaunchResponse} from '@vibesys/runs/intents';
import {assign, setup} from 'xstate';

/** No automatic retry: a lost reply requires observing the writer's new epoch. */
export const relaunchMachine = setup({
  types: {
    context: {} as {
      readonly request: RelaunchRequest;
      readonly result: RelaunchResponse | null;
    },
    input: {} as {readonly request: RelaunchRequest},
    events: {} as {readonly type: 'relaunch'},
  },
  actors: {launch: unprovided<RelaunchResponse, RelaunchRequest>('launch')},
  delays: {deadline: 35_000},
}).createMachine({
  id: 'relaunch',
  context: ({input}) => ({request: copyRelaunch(input.request), result: null}),
  initial: 'ready',
  states: {
    ready: {on: {relaunch: 'launching'}},
    launching: {
      invoke: {
        id: 'launch',
        src: 'launch',
        input: ({context}) => context.request,
        onDone: {
          target: 'settled',
          actions: assign(({event}) => ({result: event.output})),
        },
        onError: {target: 'uncertain'},
      },
      after: {deadline: 'uncertain'},
    },
    settled: {},
    uncertain: {},
  },
});
