/**
 * The app shell in the main process: it raises a notification when a watched run finishes, fails
 * or pauses, while notifications are enabled in Settings.
 */
import type {HostKey, HostListing} from '@vibesys/hosts';
import {type RunNotice, runNotices} from '../state/index.js';

export interface RunNotifierDeps {
  /** Whether Settings enables notifications now. */
  readonly enabled: () => boolean;
  /** Show `notice` (an Electron `Notification` in the app). */
  readonly notify: (notice: RunNotice) => void;
}

export class RunNotifier {
  readonly #deps: RunNotifierDeps;
  readonly #last = new Map<HostKey, HostListing>();

  constructor(deps: RunNotifierDeps) {
    this.#deps = deps;
  }

  /** Fold one listing a host's watch pushed, notifying for what changed since the last one. */
  listing(listing: HostListing): void {
    const notices = runNotices(this.#last.get(listing.host), listing);
    // An unavailable listing keeps the last one known, so a reconnect does not notify twice.
    if (listing.kind === 'listed') this.#last.set(listing.host, listing);
    if (!this.#deps.enabled()) return;
    for (const notice of notices) this.#deps.notify(notice);
  }
}
