/**
 * The sidebar's shell: its store, fed by the welcome bridge's overview and pushes. Every change is
 * a push the main process sends (a host's listing, Recent, the preferences), so there is no refresh.
 */
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import {createStore, only, type Store} from '@vibesys/store';
import {
  type ShellActions,
  SIDEBAR_EMPTY,
  type SidebarAction,
  type SidebarState,
  sidebarStep,
} from '../../state/index.js';

/** The bridge calls and pushes the sidebar uses. */
export type ShellBridge = Pick<
  WelcomeBridge,
  'overview' | 'preferences' | 'archive' | 'onListing' | 'onRecent' | 'onPreferences'
>;

/**
 * The sidebar store over `bridge`. Pushes that arrive before the overview are ordered by their
 * sequence: one at or before the overview's cursor is already in it and is dropped.
 */
export function createSidebarStore(bridge: ShellBridge): {
  readonly store: Store<SidebarState, SidebarAction>;
  readonly actions: ShellActions;
} {
  const store = createStore<SidebarState, SidebarAction, never>(
    (state, action) => only(sidebarStep(state, action)),
    SIDEBAR_EMPTY,
    () => {},
  );
  let cursor: number | null = null;
  const early: {sequence: number; action: SidebarAction}[] = [];
  const pushed = (sequence: number, action: SidebarAction): void => {
    if (cursor === null) early.push({sequence, action});
    else if (sequence > cursor) store.dispatch(action);
  };
  bridge.onListing(event => pushed(event.sequence, {type: 'listing', listing: event.value}));
  bridge.onRecent(event => pushed(event.sequence, {type: 'recent', recent: event.value}));
  bridge.onPreferences(event => store.dispatch({type: 'archived', archived: event.value.archived}));
  void settled(bridge.overview()).then(result => {
    if (!result.ok) return;
    const {listings, recent, eventCursor} = result.value;
    cursor = eventCursor;
    store.dispatch({type: 'overview', listings, recent});
    for (const {sequence, action} of early.splice(0))
      if (sequence > eventCursor) store.dispatch(action);
  });
  void settled(bridge.preferences()).then(result => {
    if (result.ok) store.dispatch({type: 'archived', archived: result.value.archived});
  });
  return {
    store,
    actions: {archive: run => void settled(bridge.archive(run, true))},
  };
}
