/**
 * The dashboard's grouping and machine filter as a `Store` a view subscribes to: the grouping
 * machine run as an actor for the life of the window.
 */
import {actorStore} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {createActor} from 'xstate';
import {type GroupingContext, type GroupingEvent, groupingMachine} from '../../state/index.js';

/** What the runs are grouped by and filtered to, and the way to change them. */
export type GroupingStore = Store<GroupingContext, GroupingEvent>;

export function createGroupingStore(): GroupingStore {
  const actor = createActor(groupingMachine).start();
  return actorStore(actor, snapshot => snapshot.context);
}
