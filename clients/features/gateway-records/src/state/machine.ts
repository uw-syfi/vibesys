/** Per-host opt-in and one raw record subscription, cancelled by leaving the active state. */
import {unprovided} from '@vibesys/machine';
import {type RecordRow, recordRows} from '@vibesys/run-state/records';
import {assign, fromCallback, setup} from 'xstate';

export type GatewayRecordsEvent =
  | {type: 'toggle'; enabled: boolean}
  | {type: 'records'; records: readonly RecordRow[]}
  | {type: 'failed'; error: string};
export const gatewayRecordsMachine = setup({
  types: {
    context: {} as {enabled: boolean; rows: readonly RecordRow[]; error: string | null},
    events: {} as GatewayRecordsEvent,
  },
  actors: {
    read: unprovided<boolean>('read transport setting'),
    save: unprovided<boolean, {enabled: boolean}>('save transport setting'),
    tail: fromCallback<GatewayRecordsEvent>(() => {
      throw new Error('tail was not provided');
    }),
  },
}).createMachine({
  id: 'gatewayRecords',
  initial: 'checking',
  context: {enabled: false, rows: [], error: null},
  states: {
    checking: {
      invoke: {
        id: 'read',
        src: 'read',
        onDone: [
          {guard: ({event}) => event.output, target: 'active', actions: assign({enabled: true})},
          {target: 'off'},
        ],
        onError: {target: 'failed', actions: assign({error: ({event}) => String(event.error)})},
      },
      after: {15000: {target: 'failed', actions: assign({error: 'Timeout'})}},
    },
    off: {on: {toggle: {target: 'saving', actions: assign({enabled: ({event}) => event.enabled})}}},
    saving: {
      invoke: {
        id: 'save',
        src: 'save',
        input: ({context}) => ({enabled: context.enabled}),
        onDone: [
          {guard: ({event}) => event.output, target: 'active'},
          {target: 'off', actions: assign({rows: []})},
        ],
        onError: {target: 'failed', actions: assign({error: ({event}) => String(event.error)})},
      },
      after: {15000: {target: 'failed', actions: assign({error: 'Timeout'})}},
    },
    active: {
      invoke: {
        id: 'tail',
        src: 'tail',
        onError: {target: 'failed', actions: assign({error: ({event}) => String(event.error)})},
      },
      on: {
        toggle: {target: 'saving', actions: assign({enabled: ({event}) => event.enabled})},
        records: {
          actions: assign({rows: ({context, event}) => recordRows(context.rows, event.records)}),
        },
        failed: {target: 'failed', actions: assign({error: ({event}) => event.error})},
      },
    },
    failed: {
      on: {
        toggle: {
          target: 'saving',
          actions: assign({enabled: ({event}) => event.enabled, error: null}),
        },
      },
    },
  },
});
