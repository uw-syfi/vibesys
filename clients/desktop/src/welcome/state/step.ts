/**
 * The shape every welcome-view screen store shares (the functional core's half).
 *
 * A screen's behavior is a pure reducer, `step(state, action) -> {state, requests}`, the same shape
 * as the connection supervisor: no I/O, clock, or DOM. Every request the reducer emits that waits
 * for a reply carries a token, and the state that awaits the reply stores that token; a reply whose
 * token is not the awaited one is dropped by the reducer, so a stale reply cannot overwrite newer
 * state. Tokens are numbers drawn from the state's own counter (`next`), so the reducer stays pure.
 * The shell (`effects/`) performs the requests and dispatches the replies.
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
