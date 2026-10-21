/** Bounded reconnect policy. A ready session owns all live Tail calls until it is lost. */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';

// Cold provisioning may transfer a multi-megabyte artifact before Hello can begin.
export const GATEWAY_COMMAND_TIMEOUT_MS = 30_000;
export const GATEWAY_UPLOAD_TIMEOUT_MS = 90_000;
const GATEWAY_DIAL_TIMEOUT_MS = GATEWAY_UPLOAD_TIMEOUT_MS + 3 * GATEWAY_COMMAND_TIMEOUT_MS;

export const gatewayMachine = setup({
  types: {
    context: {} as {attempt: number; error: string},
    events: {} as {type: 'lost'} | {type: 'close'},
  },
  actors: {
    dial: unprovided<{ok: true} | {ok: false; fatal: boolean; error: string}>('gateway dial'),
  },
  actions: {connected: () => {}, disconnected: () => {}},
  delays: {
    dialDeadline: GATEWAY_DIAL_TIMEOUT_MS,
    backoff: ({context}) => Math.min(250 * 2 ** context.attempt, 4_000),
  },
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
      after: {dialDeadline: {target: 'failed', actions: assign({error: 'gateway dial timed out'})}},
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
