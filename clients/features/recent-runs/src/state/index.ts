/**
 * The recent-runs feature's pure core: the lists the main process pushes (the recent runs and each
 * watched host's runs) and each recent run's status from its host's listing. It asks nothing, so
 * it declares no bridge.
 */
export {
  LISTS_EMPTY,
  type ListsAction,
  type ListsState,
  listsInvariants,
  listsStep,
  statusOf,
} from './lists.js';
