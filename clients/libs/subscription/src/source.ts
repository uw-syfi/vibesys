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

/** Whether two window states show a screen the same thing, without computing what they show. */
function sameView(left: WindowState, right: WindowState): boolean {
  if (!sameControl(left.control, right.control) || left.fold.broken !== right.fold.broken)
    return false;
  // A snapshot under construction, or data while main says there is no value, shows nothing.
  const shows = right.control.kind === 'live' || right.control.kind === 'stale';
  return !shows || left.fold.map === right.fold.map;
}

export function subscriptionSource<S>(
  port: SubscriptionPort,
  host: HostKey,
  topic: Topic<S>,
  key?: string,
): SubscriptionSource<S> {
  const listeners = new Set<() => void>();
  let call: {cancel(): void} | null = null;
  let window: WindowState = INITIAL_WINDOW;
  // The window at the last notification, and the state computed from it. The state is computed
  // when a listener reads it, not when an update arrives, so a burst of updates before a read
  // costs one `topic.fold` and not one per update.
  let notified: WindowState = INITIAL_WINDOW;
  let read: {readonly of: WindowState; readonly state: SubscriptionState<S>} = {
    of: INITIAL_WINDOW,
    state: NOT_OBSERVED,
  };

  const publish = (next: WindowState): void => {
    window = next;
    if (sameView(notified, next)) return;
    notified = next;
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
    notified = INITIAL_WINDOW;
    read = {of: INITIAL_WINDOW, state: NOT_OBSERVED};
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
    getSnapshot() {
      if (!sameView(read.of, window)) read = {of: window, state: stateOf(topic, window)};
      return read.state;
    },
  };
}
