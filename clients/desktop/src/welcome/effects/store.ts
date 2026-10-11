/**
 * The shell that runs one screen store: it holds the current state, runs each request the reducer
 * emits through `perform` (which talks to the main process and dispatches the reply as an action),
 * and notifies subscribers (React's `useSyncExternalStore`).
 */
import type {Reducer} from '../state/step.js';

export interface Store<S, A> {
  getState(): S;
  subscribe(listener: () => void): () => void;
  dispatch(action: A): void;
}

export function createStore<S, A, R>(
  reducer: Reducer<S, A, R>,
  initial: S,
  perform: (request: R, dispatch: (action: A) => void) => void,
): Store<S, A> {
  let state = initial;
  const listeners = new Set<() => void>();
  const dispatch = (action: A): void => {
    const next = reducer(state, action);
    if (next.state !== state) {
      state = next.state;
      for (const listener of listeners) listener();
    }
    for (const request of next.requests) perform(request, dispatch);
  };
  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispatch,
  };
}
