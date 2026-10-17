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
  type Route,
  type RunModel,
  type RunPageAction,
  type RunPageSeed,
  type RunPageState,
  type RunRecord,
  sameRecord,
  type ZoomEvent,
} from '../../state/index.js';
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
  /** The run fold (the transcript the turns read); null before the run reached the window. */
  readonly core: CoreState | null;
  /** Where the screen is zoomed, and the way to move it. */
  readonly route: Route;
  readonly zoom: (event: ZoomEvent) => void;
} | null {
  const {seed, record, bridge, navigation, schedule, page} = options;
  const [store, setStore] = useState<RunPageStore | null>(null);
  const key = `${seed.identity.host} ${seed.identity.runId}`;
  // biome-ignore lint/correctness/useExhaustiveDependencies: the store belongs to one run (`key`). Listing `seed`, `bridge`, `navigation` and `schedule` would dispose the store, its followers and its stop bound whenever the caller passes a new but equal object; a ref copy of them would hide the same choice less visibly. The record reaches the store through the effect below.
  useEffect(() => {
    let created: RunPageStore | null = null;
    const instance = (): string | null => {
      const current = created?.getState().record;
      return current === undefined || current.kind === 'unlisted' ? null : current.run.id;
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
  if (store === null || state === null) return null;
  const at = state.view.at === 'live' ? null : state.view.at;
  const model = reader.read(state.run.trace, at);
  return {state, dispatch: store.dispatch, model, core: state.run.core, route, zoom};
}
