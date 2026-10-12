/** The browser service a live session wakes on. */
import type {BrowserLifecycle, PageVisibility} from '@vibesys/run-session';

/** The document facts and event target the lifecycle adapter consumes. */
export interface BrowserDocument {
  readonly visibilityState: PageVisibility;
  addEventListener(type: 'visibilitychange', listener: () => void): void;
  removeEventListener(type: 'visibilitychange', listener: () => void): void;
}

/** The window event target the lifecycle adapter consumes for network changes. */
export interface BrowserWindow {
  addEventListener(type: 'online' | 'offline', listener: () => void): void;
  removeEventListener(type: 'online' | 'offline', listener: () => void): void;
}

/** The browser reachability fact the lifecycle adapter consumes. */
export interface BrowserNavigator {
  readonly onLine: boolean;
}

/**
 * Adapt browser lifecycle facts into the runtime-neutral session port.
 *
 * Visibility belongs to the document, while network reachability belongs to
 * the window. Keeping those targets here prevents the session from naming DOM
 * objects and gives the adapter a deterministic test seam.
 */
export function createBrowserLifecycle(
  page: BrowserDocument,
  events: BrowserWindow,
  network: BrowserNavigator,
): BrowserLifecycle {
  return {
    get visibilityState() {
      return page.visibilityState;
    },
    get online() {
      return network.onLine;
    },
    addEventListener(type, listener) {
      if (type === 'visibilitychange') page.addEventListener(type, listener);
      else events.addEventListener(type, listener);
    },
    removeEventListener(type, listener) {
      if (type === 'visibilitychange') page.removeEventListener(type, listener);
      else events.removeEventListener(type, listener);
    },
  };
}

/** The page's own visibility and network reachability. */
export function browserLifecycle(): BrowserLifecycle {
  return createBrowserLifecycle(document, window, navigator);
}
