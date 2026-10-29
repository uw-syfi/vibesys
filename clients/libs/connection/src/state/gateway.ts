/** Bounded reconnect policy. A ready session owns all live Tail calls until it is lost. */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';

// Cold provisioning may transfer a multi-megabyte artifact before Hello can begin.
export const GATEWAY_COMMAND_TIMEOUT_MS = 30_000;
export const GATEWAY_UPLOAD_TIMEOUT_MS = 90_000;
const GATEWAY_DIAL_TIMEOUT_MS = GATEWAY_UPLOAD_TIMEOUT_MS + 3 * GATEWAY_COMMAND_TIMEOUT_MS;

/**
 * The longest one recovery may take before the session is declared offline: a first connection
 * may have to upload the artifact, a reconnect to a host that already holds it may not.
 */
export const GATEWAY_FIRST_CONNECT_BUDGET_MS = 6 * 60_000;
const GATEWAY_RECONNECT_BUDGET_MS = 2 * 60_000;

export const gatewayMachine = setup({
  types: {
    context: {} as {attempt: number; error: string; provisioned: boolean},
    events: {} as {type: 'lost'} | {type: 'close'},
  },
  actors: {
    dial: unprovided<{ok: true} | {ok: false; fatal: boolean; error: string}>('gateway dial'),
  },
  actions: {connected: () => {}, disconnected: () => {}},
  delays: {
    dialDeadline: GATEWAY_DIAL_TIMEOUT_MS,
    backoff: ({context}) => Math.min(250 * 2 ** context.attempt, 4_000),
    connectBudget: ({context}) =>
      context.provisioned ? GATEWAY_RECONNECT_BUDGET_MS : GATEWAY_FIRST_CONNECT_BUDGET_MS,
  },
}).createMachine({
  id: 'gateway',
  initial: 'connecting',
  context: {attempt: 0, error: '', provisioned: false},
  on: {close: {target: '.closed'}},
  states: {
    // Dialing, failing and backing off share one budget, so a dial that never answers cannot
    // keep a lost session "reconnecting" past it.
    connecting: {
      initial: 'dialing',
      after: {
        connectBudget: {
          target: 'offline',
          actions: assign({error: 'gateway did not connect in time'}),
        },
      },
      states: {
        dialing: {
          invoke: {
            id: 'dial',
            src: 'dial',
            onDone: [
              {guard: ({event}) => event.output.ok, target: '#gateway.ready'},
              {
                guard: ({event}) => !event.output.ok && event.output.fatal,
                target: '#gateway.offline',
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
          after: {
            dialDeadline: {target: 'failed', actions: assign({error: 'gateway dial timed out'})},
          },
        },
        failed: {
          always: [
            {guard: ({context}) => context.attempt >= 4, target: '#gateway.offline'},
            {target: 'backoff', actions: assign({attempt: ({context}) => context.attempt + 1})},
          ],
        },
        backoff: {after: {backoff: 'dialing'}},
      },
    },
    ready: {
      entry: [assign({attempt: 0, provisioned: true}), 'connected'],
      exit: 'disconnected',
      on: {lost: 'connecting.failed'},
    },
    offline: {type: 'final'},
    closed: {type: 'final'},
  },
});
