/**
 * The dashboard's grouping as a `Store` a view subscribes to: the grouping machine run as an actor
 * for the life of the window.
 */
import {actorStore} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {createActor} from 'xstate';
import {type GroupBy, type GroupingEvent, groupingMachine} from '../../state/index.js';

/** What the runs are grouped by, and the way to change it. */
export type GroupingStore = Store<GroupBy, GroupingEvent>;

export function createGroupingStore(): GroupingStore {
  const actor = createActor(groupingMachine).start();
  return actorStore(actor, snapshot => snapshot.context.by);
}
