/**
 * The welcome view's wiring: each feature's store over the welcome preload's bridge (each feature
 * sees only the requests it declares), and the main process's events fanned out to the stores that
 * fold them. This is the only module of the app that talks to the bridge. Nothing here polls: host
 * data arrives as pushes (`onListing`).
 */

import type {Store} from '@vibesys/core-state';
import type {HostKey, HostListing, RecentRun} from '@vibesys/entities';
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
import {
  type ChromeState,
  settled,
  type WelcomeBridge,
  type WelcomeEvent,
  type WelcomeOverview,
  type WelcomeResult,
} from '@vibesys/ipc';

export interface WelcomeStores {
  readonly welcome: Store<WelcomeState, WelcomeAction>;
  readonly lists: Store<ListsState, ListsAction>;
  readonly host: Store<HostState, HostAction>;
  readonly start: Store<StartState, StartAction>;
  readonly picker: Store<PickerState, PickerAction>;
  readonly completion: Store<CompletionState, CompletionAction>;
}

type WelcomePush =
  | ({readonly kind: 'chrome'} & WelcomeEvent<ChromeState>)
  | ({readonly kind: 'recent'} & WelcomeEvent<readonly RecentRun[]>)
  | ({readonly kind: 'listing'} & WelcomeEvent<HostListing>)
  | ({readonly kind: 'show-host'} & WelcomeEvent<HostKey>);

/** Build the stores over `bridge`, showing `firstHost`'s panel. `confirm` asks before a resume. */
function createWelcomeStores(
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
  host.dispatch({type: 'show', host: firstHost});
  return {welcome, lists, host, start, picker, completion};
}

function applyPush(stores: WelcomeStores, push: WelcomePush): void {
  switch (push.kind) {
    case 'chrome':
      stores.welcome.dispatch({type: 'chrome', chrome: push.value});
      stores.host.dispatch({type: 'chrome', chrome: push.value});
      if (push.value.mode === 'run') {
        stores.start.dispatch({type: 'run-shown'});
        stores.picker.dispatch({type: 'close'});
      }
      return;
    case 'recent':
      stores.lists.dispatch({type: 'runs', runs: push.value});
      return;
    case 'listing':
      stores.lists.dispatch({type: 'listing', listing: push.value});
      return;
    case 'show-host':
      stores.host.dispatch({type: 'show', host: push.value});
      return;
  }
}

function applyOverview(stores: WelcomeStores, overview: WelcomeOverview): void {
  applyPush(stores, {kind: 'chrome', sequence: overview.eventCursor, value: overview.chrome});
  applyPush(stores, {kind: 'recent', sequence: overview.eventCursor, value: overview.recent});
  for (const listing of overview.listings) {
    applyPush(stores, {kind: 'listing', sequence: overview.eventCursor, value: listing});
  }
  if (overview.settingsProblem !== null) {
    stores.welcome.dispatch({type: 'problem', text: overview.settingsProblem});
  }
}

/**
 * Build the stores from the main process's overview and keep them current with its events.
 * `confirm` asks before a resume.
 *
 * Subscribes before asking for the overview, then replays every push that crossed that request.
 * The main process captures the overview and its event cursor atomically after every asynchronous
 * read, including the latest value of each push-only host listing, so events through that cursor
 * are covered by the overview; replaying only later events leaves no gap and cannot repeat an
 * effect already represented by the snapshot. A failed overview is returned and later events are
 * dropped.
 */
export async function loadWelcomeStores(
  bridge: WelcomeBridge,
  confirm: Confirm,
): Promise<WelcomeResult<WelcomeStores>> {
  const buffered: WelcomePush[] = [];
  let receive = (push: WelcomePush): void => {
    buffered.push(push);
  };
  bridge.onChrome(event => receive({kind: 'chrome', ...event}));
  bridge.onRecent(event => receive({kind: 'recent', ...event}));
  bridge.onListing(event => receive({kind: 'listing', ...event}));
  bridge.onShowHost(event => receive({kind: 'show-host', ...event}));

  const overview = await settled(bridge.overview());
  if (!overview.ok) {
    buffered.length = 0;
    receive = () => undefined;
    return overview;
  }

  const stores = createWelcomeStores(bridge, confirm, overview.value.host);
  applyOverview(stores, overview.value);
  for (const push of buffered) {
    if (push.sequence > overview.value.eventCursor) applyPush(stores, push);
  }
  buffered.length = 0;
  receive = push => applyPush(stores, push);
  return {ok: true, value: stores};
}
