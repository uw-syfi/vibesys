/**
 * How the Runs dashboard groups its tiles, as a machine (docs/contributing/clients-architecture.md,
 * "State"): one mode, `ready`, whose context is what the runs are grouped by. Choosing the same
 * grouping again changes nothing.
 */
import {assign, setup} from 'xstate';
import type {GroupBy} from './tiles.js';

export interface GroupingContext {
  readonly by: GroupBy;
}

export type GroupingEvent = {readonly type: 'group'; readonly by: GroupBy};

export const groupingMachine = setup({
  types: {context: {} as GroupingContext, events: {} as GroupingEvent},
  actions: {choose: assign((_, params: {readonly by: GroupBy}) => ({by: params.by}))},
}).createMachine({
  id: 'runsGrouping',
  context: {by: 'status'},
  initial: 'ready',
  states: {
    ready: {
      on: {
        group: {actions: {type: 'choose', params: ({event}) => ({by: event.by})}},
      },
    },
  },
});
