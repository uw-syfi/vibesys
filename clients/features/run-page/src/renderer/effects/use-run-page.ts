/**
 * The Run screen's store for as long as the screen shows one run: created when it mounts, fed the
 * run's record as the host's watch pushes it, and disposed (followers stopped, the bound cancelled,
 * the logs channel let go) when it unmounts or shows another run.
 */
import type {ScheduleTimeout} from '@vibesys/machine';
import type {CoreState} from '@vibesys/run-state';
import {useEffect, useMemo, useState, useSyncExternalStore} from 'react';
import {
  createRunReader,
  logStreamToFollow,
  type Route,
  type RunModel,
  type RunPageAction,
  type RunPageSeed,
  type RunPageState,
  type RunRecord,
  runEnding,
  sameRecord,
  type ZoomEvent,
} from '../../state/index.js';
import type {LiveChat} from './live-session.js';
import {createRunPageStore, type RunPageStore} from './store.js';
import {useZoom} from './use-zoom.js';
import {
  type RunPageBridge,
  type RunPageNavigation,
  type RunPagePage,
  welcomeRunPorts,
} from './welcome-ports.js';

export interface RunPageOptions {
  readonly seed: RunPageSeed;
  readonly record: RunRecord;
  readonly bridge: RunPageBridge;
  readonly navigation: RunPageNavigation;
  readonly schedule: ScheduleTimeout;
  /** The page the run's session runs in (its document, window and navigator). */
  readonly page: RunPagePage;
}

const NO_STORE = {
  subscribe: () => () => {},
  getState: () => null,
};

/** The screen's state and dispatch, or null on the first render before its store exists. */
export function useRunPage(options: RunPageOptions): {
  readonly state: RunPageState;
  readonly dispatch: (action: RunPageAction) => void;
  /** The run at the view's sequence, read by the page's own reader (`createRunReader`). */
  readonly model: RunModel;
  /** The run to its live end whatever the view's sequence: what the chart reads, so scrubbing never changes it. */
  readonly whole: RunModel;
  /** The run fold (the transcript the turns read); null before the run reached the window. */
  readonly core: CoreState | null;
  /** Where the screen is zoomed, and the way to move it. */
  readonly route: Route;
  readonly zoom: (event: ZoomEvent) => void;
  /** The attached run's chat over its session. */
  readonly chat: LiveChat;
} | null {
  const {seed, record, bridge, navigation, schedule, page} = options;
  const [store, setStore] = useState<RunPageStore | null>(null);
  const key = `${seed.identity.host} ${seed.identity.runId}`;
  // biome-ignore lint/correctness/useExhaustiveDependencies: the store belongs to one run (`key`). Listing `seed`, `bridge`, `navigation` and `schedule` would dispose the store, its followers and its stop bound whenever the caller passes a new but equal object; a ref copy of them would hide the same choice less visibly. The record reaches the store through the effect below.
  useEffect(() => {
    let created: RunPageStore | null = null;
    const instance = (): string | null => {
      const current = created?.getState().record;
      return current === undefined || current.kind === 'unlisted' || current.kind === 'historical'
        ? null
        : current.run.id;
    };
    const ports = welcomeRunPorts(
      bridge,
      {run: seed.identity, project: seed.title.project, instance},
      navigation,
      page,
    );
    created = createRunPageStore(seed, ports, schedule);
    setStore(created);
    const owned = created;
    return () => {
      owned.dispose();
      ports.releaseLogs();
      setStore(null);
    };
  }, [key]);
  useEffect(() => {
    if (store !== null && !sameRecord(store.getState().record, record)) {
      store.dispatch({type: 'record', record});
    }
  }, [store, record]);
  const source = store ?? NO_STORE;
  const state = useSyncExternalStore(source.subscribe, source.getState, source.getState);
  // biome-ignore lint/correctness/useExhaustiveDependencies: the reader caches one run's folds (`key`); a new run starts a new reader.
  const reader = useMemo(() => createRunReader(), [key]);
  const {route, zoom} = useZoom(key, page.address);
  const ending = state === null ? null : runEnding(state);
  const model =
    state === null
      ? null
      : reader.read(state.run.trace, state.view.at === 'live' ? null : state.view.at, ending);
  // Read after `model`: at the live end it is the same object, and at a past sequence it is the
  // live fold the reader keeps (the past model it also keeps stays for the next scrub).
  const whole = state === null ? null : reader.read(state.run.trace, null, ending);
  // The page fetches lines only for its selected stream: while a Logs face is on screen, select
  // the stream it shows. Once selected the function returns null, so this dispatches once per
  // stream the face moves to.
  const follow = state === null || model === null ? null : logStreamToFollow(state, model, route);
  useEffect(() => {
    if (store !== null && follow !== null)
      store.dispatch({type: 'select-stream', streamId: follow});
  }, [store, follow]);
  // The summary already holds the agent and round lists. Only an opened step needs its journal
  // transcript; on a modern helper the span index reads that step alone.
  const detailSpan = state?.summary.kind === 'ready' ? route.step : null;
  useEffect(() => {
    if (store !== null && detailSpan !== null)
      store.dispatch({type: 'need-span', spanId: detailSpan});
  }, [store, detailSpan]);
  if (store === null || state === null || model === null) return null;
  if (whole === null) return null;
  return {
    state,
    dispatch: store.dispatch,
    model,
    whole,
    core: state.run.core,
    route,
    zoom,
    chat: store.chat,
  };
}
