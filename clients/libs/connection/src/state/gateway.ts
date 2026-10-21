/** Bounded reconnect policy. A ready session owns all live Tail calls until it is lost. */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';

export const gatewayMachine = setup({
  types: {
    context: {} as {attempt: number; error: string},
    events: {} as {type: 'lost'} | {type: 'close'},
  },
  actors: {
    dial: unprovided<{ok: true} | {ok: false; fatal: boolean; error: string}>('gateway dial'),
  },
  actions: {connected: () => {}, disconnected: () => {}},
  delays: {backoff: ({context}) => Math.min(250 * 2 ** context.attempt, 4_000)},
}).createMachine({
  id: 'gateway',
  initial: 'dialing',
  context: {attempt: 0, error: ''},
  on: {close: {target: '.closed'}},
  states: {
    dialing: {
      invoke: {
        id: 'dial',
        src: 'dial',
        onDone: [
          {guard: ({event}) => event.output.ok, target: 'ready'},
          {
            guard: ({event}) => !event.output.ok && event.output.fatal,
            target: 'offline',
            actions: assign({error: ({event}) => (event.output.ok ? '' : event.output.error)}),
          },
          {
            target: 'failed',
            actions: assign({error: ({event}) => (event.output.ok ? '' : event.output.error)}),
          },
        ],
        onError: {
          target: 'failed',
          actions: assign({error: ({event}) => String(event.error)}),
        },
      },
      after: {30000: {target: 'failed', actions: assign({error: 'gateway dial timed out'})}},
    },
    ready: {entry: [assign({attempt: 0}), 'connected'], exit: 'disconnected', on: {lost: 'failed'}},
    failed: {
      always: [
        {guard: ({context}) => context.attempt >= 4, target: 'offline'},
        {target: 'backoff', actions: assign({attempt: ({context}) => context.attempt + 1})},
      ],
    },
    backoff: {after: {backoff: 'dialing'}},
    offline: {type: 'final'},
    closed: {type: 'final'},
  },
});
