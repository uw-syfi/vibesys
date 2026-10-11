/** The browser service a live session wakes on. */
import type {BrowserLifecycle} from '@vibesys/run-session';

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
