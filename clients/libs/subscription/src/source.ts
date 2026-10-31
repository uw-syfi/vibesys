/**
 * A window's subscription as an external store, for React's `useSyncExternalStore`: the first
 * listener opens a port to main, and the last one to leave closes it, which stops main's interest.
 * The window has no timers: main owns the clock and the retries, and a closed port is `failed`.
 */
import type {HostKey} from '@vibesys/hosts';
import type {SubscribeRequest} from '@vibesys/proto';
import type {SubscriptionUpdate} from './fold.js';
import {CONNECTING, type SubscriptionState} from './state.js';
import type {Topic} from './topics.js';
import {applyUpdate, INITIAL_WINDOW, sameControl, stateOf, type WindowState} from './view.js';

/** What a window has of main: one port per observed topic, carrying decoded updates. */
export interface SubscriptionPort {
  observe(
    host: HostKey,
    request: SubscribeRequest,
    key: string | undefined,
    receive: (update: SubscriptionUpdate) => void,
    /** The port is over: main cancelled it, closed it, or could not deliver. */
    ended: (error?: string) => void,
  ): {cancel(): void};
}

export interface SubscriptionSource<S> {
  subscribe(listener: () => void): () => void;
  /** The same object until the state changes, as `useSyncExternalStore` requires. */
  getSnapshot(): SubscriptionState<S>;
}

const NOT_OBSERVED: SubscriptionState<never> = CONNECTING;

export function subscriptionSource<S>(
  port: SubscriptionPort,
  host: HostKey,
  topic: Topic<S>,
  key?: string,
): SubscriptionSource<S> {
  const listeners = new Set<() => void>();
  let call: {cancel(): void} | null = null;
  let window: WindowState = INITIAL_WINDOW;
  let current: SubscriptionState<S> = NOT_OBSERVED;

  const publish = (next: WindowState): void => {
    const previous = window;
    window = next;
    // What the screen cannot see does not wake it: a snapshot under construction, or data while
    // main says there is no value to show.
    const shows = next.control.kind === 'live' || next.control.kind === 'stale';
    if (
      sameControl(previous.control, next.control) &&
      previous.fold.broken === next.fold.broken &&
      (!shows || previous.fold.map === next.fold.map)
    )
      return;
    current = stateOf(topic, next);
    for (const listener of [...listeners]) listener();
  };

  const open = (): void => {
    call = port.observe(
      host,
      topic.request,
      key,
      update => publish(applyUpdate(topic, window, update)),
      () => publish({...window, control: {kind: 'failed', reason: 'closed', retrying: false}}),
    );
  };
  const close = (): void => {
    call?.cancel();
    call = null;
    window = INITIAL_WINDOW;
    current = NOT_OBSERVED;
  };

  return {
    subscribe(listener) {
      listeners.add(listener);
      if (call === null) open();
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) close();
      };
    },
    getSnapshot: () => current,
  };
}
