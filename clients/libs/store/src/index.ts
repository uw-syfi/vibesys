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
 * action), and notifies subscribers (React's `useSyncExternalStore`). A transition commits state,
 * completes isolated subscriber delivery, and hands off every request before subscriber failures
 * return to the outermost synchronous dispatch caller.
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

/**
 * A subscriber collection whose delivery is isolated from each subscriber's failure and mutation.
 * Each `subscribe` call is one independent subscription, even when several use the same callback.
 * `notify` visits every subscription present when that delivery began exactly once: unsubscribing
 * one during delivery affects only later notifications, while a new subscription waits for the
 * next notification. A reentrant notification takes its own snapshot and completes before the
 * interrupted notification continues.
 *
 * Every eligible subscriber is called. One failure is rethrown by identity after delivery; more
 * than one is reported as an `AggregateError` in delivery order.
 */
export interface ListenerDelivery {
  subscribe(listener: () => void): () => void;
  notify(): void;
}

function combinedFailure(
  failures: readonly unknown[],
  message: string,
): {readonly cause: unknown} | null {
  if (failures.length === 0) return null;
  if (failures.length === 1) return {cause: failures[0]};
  return {cause: new AggregateError(failures, message)};
}

/** Defer subscriber failures until the outermost synchronous dispatch has delivered its work. */
class DispatchFailures {
  #depth = 0;
  #subscribers: unknown[] = [];

  begin(): boolean {
    const owner = this.#depth === 0;
    if (owner) this.#subscribers = [];
    this.#depth += 1;
    return owner;
  }

  notify(delivery: ListenerDelivery): void {
    try {
      delivery.notify();
    } catch (cause) {
      this.#subscribers.push(cause);
    }
  }

  finish(owner: boolean, actionFailure: {readonly cause: unknown} | null): void {
    this.#depth -= 1;
    if (!owner) {
      if (actionFailure !== null) throw actionFailure.cause;
      return;
    }
    const subscriberFailure = combinedFailure(
      this.#subscribers,
      'multiple store subscriber deliveries failed',
    );
    this.#subscribers = [];
    if (actionFailure !== null && subscriberFailure !== null) {
      throw new AggregateError(
        [subscriberFailure.cause, actionFailure.cause],
        'store subscriber delivery and dispatch failed',
      );
    }
    if (actionFailure !== null) throw actionFailure.cause;
    if (subscriberFailure !== null) throw subscriberFailure.cause;
  }
}

/** Create the shared subscriber-delivery mechanism used by reducer and machine stores. */
export function createListenerDelivery(): ListenerDelivery {
  const subscriptions = new Set<{readonly listener: () => void}>();
  return {
    subscribe(listener) {
      const subscription = {listener};
      subscriptions.add(subscription);
      return () => subscriptions.delete(subscription);
    },
    notify() {
      const failures: unknown[] = [];
      for (const {listener} of [...subscriptions]) {
        try {
          listener();
        } catch (cause) {
          failures.push(cause);
        }
      }
      const failure = combinedFailure(failures, 'multiple store subscribers failed');
      if (failure !== null) throw failure.cause;
    },
  };
}

export function createStore<S, A, R>(
  reducer: Reducer<S, A, R>,
  initial: S,
  perform: (request: R, dispatch: (action: A) => void) => void,
): Store<S, A> {
  let state = initial;
  const delivery = createListenerDelivery();
  const failures = new DispatchFailures();
  const dispatch = (action: A): void => {
    const ownsFailureReport = failures.begin();
    let actionFailure: {readonly cause: unknown} | null = null;
    try {
      const next = reducer(state, action);
      if (next.state !== state) {
        state = next.state;
        failures.notify(delivery);
      }
      for (const request of next.requests) perform(request, dispatch);
    } catch (cause) {
      actionFailure = {cause};
    }
    failures.finish(ownsFailureReport, actionFailure);
  };
  return {
    getState: () => state,
    subscribe: delivery.subscribe,
    dispatch,
  };
}
