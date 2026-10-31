/**
 * The sidebar's shell: its store, fed by the welcome bridge's overview and pushes (a host's
 * listing, Recent, the preferences), and by the run digests, which the store asks for itself
 * (`SidebarRequest`).
 */
import type {HostKey} from '@vibesys/hosts';
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import {sameRun} from '@vibesys/runs';
import {createStore, type Store} from '@vibesys/store';
import {
  initialSidebar,
  liveRows,
  projectRunGaps,
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
  | 'runDigests'
  | 'projectRuns'
  | 'onListing'
  | 'onRecent'
  | 'onPreferences'
  | 'retryListing'
>;

/** Run `callback` after `delayMs`; the returned function cancels it (the app injects the scheduler). */
export type ScheduleRefresh = (callback: () => void, delayMs: number) => () => void;

/** How long a live run's digest is left before it is read again, in milliseconds. */
export const LIVE_DIGEST_REFRESH_MS = 15_000;

/** Ask what `request` names; its reply is dispatched as the action that carries its token. */
function perform(
  bridge: ShellBridge,
  request: SidebarRequest,
  dispatch: (action: SidebarAction) => void,
): void {
  const {token} = request;
  if (request.kind === 'list-project-runs') {
    const {host, project} = request;
    void settled(bridge.projectRuns(host, project)).then(result =>
      dispatch({
        type: 'project-runs-listed',
        token,
        host,
        project,
        runs: result.ok ? result.value : null,
        ...(result.ok ? {} : {error: result.error}),
      }),
    );
    return;
  }
  void settled(bridge.runDigests(request.host, request.project, request.runIds)).then(result =>
    dispatch({type: 'digests-read', token, reply: result.ok ? result.value : null}),
  );
}

/**
 * The sidebar store over `bridge`. Pushes that arrive before the overview are ordered by their
 * sequence: one at or before the overview's cursor is already in it and is dropped.
 */
export function createSidebarStore(
  bridge: ShellBridge,
  schedule: ScheduleRefresh,
): {
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
  // While any run is live its digest is due again after each interval; the next waits on this one.
  let refreshing = false;
  const keepLiveDigestsFresh = (): void => {
    const state = store.getState();
    const waiting =
      liveRows(state).length > 0 ||
      projectRunGaps(state.allRuns).length > 0 ||
      Object.values(state.digests).some(slot => slot.kind === 'failed');
    if (refreshing || !waiting) return;
    refreshing = true;
    schedule(() => {
      refreshing = false;
      store.dispatch({type: 'refresh-live-digests'});
    }, LIVE_DIGEST_REFRESH_MS);
  };
  store.subscribe(keepLiveDigestsFresh);
  keepLiveDigestsFresh();
  let cursor: number | null = null;
  const early: {sequence: number; action: SidebarAction}[] = [];
  const pushed = (sequence: number, action: SidebarAction): void => {
    if (cursor === null) early.push({sequence, action});
    else if (sequence > cursor) store.dispatch(action);
  };
  bridge.onListing(event => pushed(event.sequence, {type: 'listing', listing: event.value}));
  bridge.onRecent(event => pushed(event.sequence, {type: 'recent', recent: event.value}));
  bridge.onPreferences(event => store.dispatch({type: 'archived', archived: event.value.archived}));
  const askOverview = (): void => {
    void settled(bridge.overview()).then(result => {
      if (!result.ok) {
        store.dispatch({type: 'overview-failed', reason: result.error});
        return;
      }
      const {listings, recent, skippedRecent, eventCursor} = result.value;
      cursor = eventCursor;
      store.dispatch({type: 'overview', listings, recent, skippedRecent});
      for (const {sequence, action} of early.splice(0))
        if (sequence > eventCursor) store.dispatch(action);
    });
  };
  askOverview();
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
          store.dispatch({type: 'select', screen: {kind: 'runs'}});
        }
        void settled(bridge.openRunWindow(run));
      },
      retryLoad: (host: HostKey) => {
        if (store.getState().overview.kind === 'failed') {
          store.dispatch({type: 'overview-asked'});
          askOverview();
        }
        void settled(bridge.retryListing(host));
      },
    },
  };
}
