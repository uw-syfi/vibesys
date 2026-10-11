/**
 * The lists the main process pushes: the recent runs (the settings store is their one writer) and
 * each watched host's runs (`HostListing`, folded from the host helper's `instances.watch`). It
 * asks nothing: every change arrives as a push, so there is no refresh to forget and no timer.
 * Each recent run's status is derived from its host's listing (`recentStatus`).
 */

import type {HostKey, HostListing} from '@vibesys/hosts';
import {type RecentRun, type RecentStatus, recentStatus} from '@vibesys/runs';
import {only, type Step} from '@vibesys/store';

export interface ListsState {
  readonly runs: readonly RecentRun[];
  /** The latest listing of each host the main process watches, by host key. */
  readonly listings: Readonly<Record<HostKey, HostListing>>;
}

export type ListsAction =
  | {readonly type: 'runs'; readonly runs: readonly RecentRun[]}
  | {readonly type: 'listing'; readonly listing: HostListing};

export const LISTS_EMPTY: ListsState = {runs: [], listings: {}};

/** A recent run's status, from its host's latest listing. */
export function statusOf(state: ListsState, run: RecentRun): RecentStatus {
  return recentStatus(run, state.listings[run.host]);
}

export function listsInvariants(state: ListsState): string[] {
  return Object.entries(state.listings)
    .filter(([host, listing]) => listing.host !== host)
    .map(([host, listing]) => `listing of ${listing.host} filed under ${host}`);
}

export function listsStep(state: ListsState, action: ListsAction): Step<ListsState, never> {
  switch (action.type) {
    case 'runs':
      return only({...state, runs: action.runs});
    case 'listing':
      return only({...state, listings: {...state.listings, [action.listing.host]: action.listing}});
  }
}
