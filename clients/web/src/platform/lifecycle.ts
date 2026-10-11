/** The browser service a live session wakes on. */

/** The page's visibility and network reachability, and their change events. */
export interface BrowserLifecycle {
  readonly visibilityState: DocumentVisibilityState;
  readonly online: boolean;
  addEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
  removeEventListener(type: 'visibilitychange' | 'online' | 'offline', listener: () => void): void;
}

/** The page's own visibility and network reachability. */
export const browserLifecycle: BrowserLifecycle = {
  get visibilityState() {
    return document.visibilityState;
  },
  get online() {
    return navigator.onLine;
  },
  addEventListener(type, listener) {
    window.addEventListener(type, listener);
  },
  removeEventListener(type, listener) {
    window.removeEventListener(type, listener);
  },
};
