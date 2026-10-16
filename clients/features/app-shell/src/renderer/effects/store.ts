/**
 * The sidebar's shell: its store, fed by the welcome bridge's overview and pushes (a host's
 * listing, Recent, the preferences), and by the saved machines and their checks, which the store
 * asks for itself (`SidebarRequest`).
 */
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import {sameRun} from '@vibesys/runs';
import {createStore, type Store} from '@vibesys/store';
import {
  initialSidebar,
  type ShellActions,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  sidebarStep,
} from '../../state/index.js';

/** The bridge calls and pushes the sidebar uses. */
export type ShellBridge = Pick<
  WelcomeBridge,
  | 'overview'
  | 'preferences'
  | 'archive'
  | 'openRunWindow'
  | 'machines'
  | 'machine'
  | 'onListing'
  | 'onRecent'
  | 'onPreferences'
>;

/** Ask what `request` names; its reply is dispatched as the action that carries its token. */
function perform(
  bridge: ShellBridge,
  request: SidebarRequest,
  dispatch: (action: SidebarAction) => void,
): void {
  const {token} = request;
  if (request.kind === 'list-machines') {
    void settled(bridge.machines()).then(result =>
      dispatch({
        type: 'machines-listed',
        token,
        hosts: result.ok ? result.value.map(machine => machine.host) : null,
      }),
    );
    return;
  }
  const {host} = request;
  void settled(bridge.machine(host)).then(result =>
    dispatch({
      type: 'machine-checked',
      token,
      host,
      ready: result.ok && result.value.kind === 'ready',
    }),
  );
}

/**
 * The sidebar store over `bridge`. Pushes that arrive before the overview are ordered by their
 * sequence: one at or before the overview's cursor is already in it and is dropped.
 */
export function createSidebarStore(bridge: ShellBridge): {
  readonly store: Store<SidebarState, SidebarAction>;
  readonly actions: ShellActions;
} {
  const initial = initialSidebar();
  const store = createStore<SidebarState, SidebarAction, SidebarRequest>(
    sidebarStep,
    initial.state,
    (request, dispatch) => perform(bridge, request, dispatch),
  );
  for (const request of initial.requests) perform(bridge, request, store.dispatch);
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
    actions: {
      archive: run => void settled(bridge.archive(run, true)),
      openWindow: run => {
        const shown = store.getState().selection;
        if (shown.kind === 'run' && sameRun(shown.run, run)) {
          store.dispatch({type: 'select', screen: {kind: 'new-run'}});
        }
        void settled(bridge.openRunWindow(run));
      },
    },
  };
}
