/**
 * The recent-runs feature's shell: the lists store. Every change arrives as a push the app
 * dispatches, so the store performs no request.
 */

import {createStore, type Store} from '@vibesys/core-state';
import {LISTS_EMPTY, type ListsAction, type ListsState, listsStep} from '../../state/index.js';

export function createListsStore(): Store<ListsState, ListsAction> {
  return createStore(listsStep, LISTS_EMPTY, () => {});
}
