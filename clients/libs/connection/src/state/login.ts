/** One host login. Leaving `running` aborts the watch; a subscription is not a state. */
import {unprovided} from '@vibesys/machine';
import {setup} from 'xstate';

export type LoginWatchResult = 'completed' | 'lost';

export const loginMachine = setup({
  types: {
    events: {} as {type: 'unavailable'} | {type: 'cancel'} | {type: 'lost'} | {type: 'failed'},
  },
  actors: {
    watch: unprovided<LoginWatchResult>('login watch'),
  },
}).createMachine({
  id: 'login',
  initial: 'running',
  states: {
    running: {
      invoke: {
        src: 'watch',
        onDone: [
          {guard: ({event}) => event.output === 'lost', target: 'unresolved'},
          {target: 'completed'},
        ],
        onError: {target: 'failed'},
      },
      on: {
        unavailable: 'unavailable',
        cancel: 'cancelled',
        lost: 'unresolved',
        failed: 'failed',
      },
    },
    completed: {type: 'final'},
    unavailable: {type: 'final'},
    cancelled: {type: 'final'},
    failed: {type: 'final'},
    unresolved: {type: 'final'},
  },
});
