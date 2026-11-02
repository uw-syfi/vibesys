/**
 * The orchestrator feature's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"). A skeleton to replace: a mode (`idle`, `working`), the operation `working`
 * invokes (provided by `renderer/effects` with `machine.provide({actors})`), and a deadline as an
 * `after` delay. Leaving `working` stops the operation and cancels the deadline.
 */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';

export interface OrchestratorContext {
  readonly attempts: number;
  readonly error: string | null;
}

export type OrchestratorEvent = {readonly type: 'start'} | {readonly type: 'cancel'};

export const orchestratorMachine = setup({
  types: {context: {} as OrchestratorContext, events: {} as OrchestratorEvent},
  actors: {work: unprovided<null>('work')},
  actions: {
    countAttempt: assign(({context}) => ({attempts: context.attempts + 1, error: null})),
    fail: assign((_, params: {readonly error: string}) => ({error: params.error})),
  },
  guards: {mayStart: ({context}) => context.attempts < 3},
  delays: {deadline: 10_000},
}).createMachine({
  id: 'orchestrator',
  context: {attempts: 0, error: null},
  initial: 'idle',
  states: {
    idle: {on: {start: {guard: 'mayStart', target: 'working', actions: 'countAttempt'}}},
    working: {
      invoke: {
        id: 'work',
        src: 'work',
        onDone: 'idle',
        onError: {
          target: 'idle',
          actions: {type: 'fail', params: ({event}) => ({error: String(event.error)})},
        },
      },
      after: {deadline: {target: 'idle', actions: {type: 'fail', params: {error: 'timed out'}}}},
      on: {cancel: 'idle'},
    },
  },
});
