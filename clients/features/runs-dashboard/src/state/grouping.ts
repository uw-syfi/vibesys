/**
 * How the Runs dashboard groups and filters its tiles, as a machine
 * (docs/contributing/clients-architecture.md, "State"): one mode, `ready`, whose context is what the
 * runs are grouped by and the one machine they are narrowed to, if any. Choosing the same grouping
 * or the same machine again changes nothing.
 */
import {assign, setup} from 'xstate';
import type {GroupBy} from './tiles.js';

export interface GroupingContext {
  readonly by: GroupBy;
  /** The machine (as a tile names it) the dashboard shows runs of, or null for every machine. */
  readonly machine: string | null;
}

export type GroupingEvent =
  | {readonly type: 'group'; readonly by: GroupBy}
  | {readonly type: 'filter-machine'; readonly machine: string}
  | {readonly type: 'clear-machine'};

export const groupingMachine = setup({
  types: {context: {} as GroupingContext, events: {} as GroupingEvent},
  actions: {
    choose: assign((_, params: {readonly by: GroupBy}) => ({by: params.by})),
    narrow: assign((_, params: {readonly machine: string | null}) => ({machine: params.machine})),
  },
}).createMachine({
  id: 'runsGrouping',
  context: {by: 'status', machine: null},
  initial: 'ready',
  states: {
    ready: {
      on: {
        group: {
          guard: ({context, event}) => context.by !== event.by,
          actions: {type: 'choose', params: ({event}) => ({by: event.by})},
        },
        'filter-machine': {
          guard: ({context, event}) => context.machine !== event.machine,
          actions: {type: 'narrow', params: ({event}) => ({machine: event.machine})},
        },
        'clear-machine': {
          guard: ({context}) => context.machine !== null,
          actions: {type: 'narrow', params: () => ({machine: null})},
        },
      },
    },
  },
});
