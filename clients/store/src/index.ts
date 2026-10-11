/**
 * The reducer kernel every data fold and screen store of every frontend shares.
 * Generic: it names no concept of the product (the package-graph checker's `domain-free` rule).
 *
 * A concern's behavior is a pure reducer, `step(state, action) -> {state, requests}`: no I/O,
 * clock, or DOM. Every request the reducer emits that waits for a reply carries a token, and the
 * state that awaits the reply stores that token; a reply whose token is not the awaited one is
 * dropped by the reducer, so a stale reply cannot overwrite newer state. Tokens are numbers drawn
 * from the state's own counter (`next`), so the reducer stays pure.
 *
 * `createStore` is the thin shell that drives one reducer: it holds the current state, hands each
 * request the reducer emits to `perform` (the frontend's effects, which dispatch the reply as an
 * action), and notifies subscribers (React's `useSyncExternalStore`).
 */

export interface Step<S, R> {
  readonly state: S;
  readonly requests: readonly R[];
}

export type Reducer<S, A, R> = (state: S, action: A) => Step<S, R>;

/** No requests: the step only changed (or kept) the state. */
export function only<S, R>(state: S): Step<S, R> {
  return {state, requests: []};
}

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
