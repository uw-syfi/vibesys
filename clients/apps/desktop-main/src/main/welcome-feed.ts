/**
 * The main process's ordered welcome feed: every push gets one sequence, and the latest values
 * needed for an authoritative bootstrap snapshot are retained beside that sequence.
 */
import type {HostKey, HostListing} from '@vibesys/hosts';
import type {PreferencesView, WelcomeEvent, WelcomeOverview} from '@vibesys/ipc';
import type {RecentRun, SkippedRecent} from '@vibesys/runs';

export interface WelcomeSnapshotValues {
  /** A disk read used until this process has published a newer Recent value. */
  readonly recent: readonly RecentRun[];
  /** The entries that disk read left out of `recent`. */
  readonly skippedRecent: readonly SkippedRecent[];
}

/** The single sequence and retained state behind the welcome snapshot-plus-stream handoff. */
export class WelcomeFeed {
  #sequence = 0;
  #recent: readonly RecentRun[] | null = null;
  readonly #listings = new Map<HostKey, HostListing>();

  hostSettings(): WelcomeEvent<null> {
    return this.#next(null);
  }

  recent(value: readonly RecentRun[]): WelcomeEvent<readonly RecentRun[]> {
    this.#recent = value;
    return this.#next(value);
  }

  preferences(value: PreferencesView): WelcomeEvent<PreferencesView> {
    return this.#next(value);
  }

  listing(value: HostListing): WelcomeEvent<HostListing> {
    this.#listings.set(value.host, value);
    return this.#next(value);
  }

  /** Forget a host that no visible panel or recent run retains. */
  retireHost(host: HostKey): void {
    this.#listings.delete(host);
  }

  /**
   * Capture all synchronous owner state and the exact sequence that covers it.
   *
   * The caller performs asynchronous reads before entering this method. A Recent push that crossed
   * those reads is retained here and wins over their older result. JavaScript runs this method as
   * one turn, so no event can fall between the returned values and cursor.
   */
  snapshot(values: WelcomeSnapshotValues): WelcomeOverview {
    return {
      ...values,
      // A published list is of a file the store wrote, which it does while every entry is readable.
      recent: this.#recent ?? values.recent,
      skippedRecent: this.#recent === null ? values.skippedRecent : [],
      listings: [...this.#listings.values()],
      eventCursor: this.#sequence,
    };
  }

  #next<T>(value: T): WelcomeEvent<T> {
    return {sequence: ++this.#sequence, value};
  }
}
