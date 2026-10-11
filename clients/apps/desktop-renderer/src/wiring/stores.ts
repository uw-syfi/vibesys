/**
 * The welcome view's wiring: each feature's store over the welcome preload's bridge (each feature
 * sees only the requests it declares), and the main process's events fanned out to the stores that
 * fold them. This is the only module of the app that talks to the bridge.
 */

import type {Store} from '@vibesys/core-state';
import type {HostKey} from '@vibesys/entities';
import {
  createCompletionStore,
  createHostStore,
  createPickerStore,
} from '@vibesys/feature-hosts/renderer';
import type {
  CompletionAction,
  CompletionState,
  HostAction,
  HostState,
  PickerAction,
  PickerState,
} from '@vibesys/feature-hosts/state';
import {createListsStore} from '@vibesys/feature-recent-runs/renderer';
import type {ListsAction, ListsState} from '@vibesys/feature-recent-runs/state';
import {type Confirm, createRunControlStore} from '@vibesys/feature-run-control/renderer';
import type {WelcomeAction, WelcomeState} from '@vibesys/feature-run-control/state';
import {createStartStore} from '@vibesys/feature-start-run/renderer';
import type {StartAction, StartState} from '@vibesys/feature-start-run/state';
import type {WelcomeBridge} from '@vibesys/ipc';

export interface WelcomeStores {
  readonly welcome: Store<WelcomeState, WelcomeAction>;
  readonly lists: Store<ListsState, ListsAction>;
  readonly host: Store<HostState, HostAction>;
  readonly start: Store<StartState, StartAction>;
  readonly picker: Store<PickerState, PickerAction>;
  readonly completion: Store<CompletionState, CompletionAction>;
}

/**
 * Build the stores over `bridge`, showing `firstHost`'s panel, and subscribe to its events.
 * `confirm` asks before a resume.
 */
export function welcomeStores(
  bridge: WelcomeBridge,
  confirm: Confirm,
  firstHost: HostKey,
): WelcomeStores {
  const welcome = createRunControlStore(bridge, confirm);
  const lists = createListsStore();
  const host = createHostStore(bridge, firstHost);
  const start = createStartStore(bridge);
  const picker = createPickerStore(bridge, key => host.dispatch({type: 'show', host: key}));
  const completion = createCompletionStore(bridge);
  bridge.onChrome(chrome => {
    welcome.dispatch({type: 'chrome', chrome});
    host.dispatch({type: 'chrome', chrome});
    if (chrome.mode === 'run') {
      start.dispatch({type: 'run-shown'});
      picker.dispatch({type: 'close'});
    }
  });
  bridge.onRecent(runs => lists.dispatch({type: 'runs', runs}));
  bridge.onListing(listing => lists.dispatch({type: 'listing', listing}));
  bridge.onShowHost(key => host.dispatch({type: 'show', host: key}));
  host.dispatch({type: 'show', host: firstHost});
  return {welcome, lists, host, start, picker, completion};
}
