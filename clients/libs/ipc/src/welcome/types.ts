/**
 * What every welcome request and event shares: the result a request answers with, the ordered
 * events the main process sends, and the payloads more than one feature reads.
 *
 * The welcome view is a trusted app page in the app's one window, but the main process still treats
 * its requests as input: a host key must parse to a usable host, paths are validated before they
 * reach a host, and no host command line is ever built from these strings. The run view never gets
 * these channels; only the welcome view's own web contents does, by the sender's identity.
 *
 * Host data reaches the view as pushes (`listing`), folded from the host helper's
 * `instances.watch`: there is no refresh request to forget, and no timer.
 */
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun, SkippedRecent} from '@vibesys/runs';

/** The events the main process sends the welcome view. */
export const WELCOME_EVENTS = {
  /** The recent runs changed (`RecentRun[]`): the settings store wrote them. */
  recent: 'vibesys:welcome:recent',
  /** A host's runs changed (`HostListing`), as its helper pushed them. */
  listing: 'vibesys:welcome:listing',
  /** The preferences changed (`PreferencesView`): the settings store wrote them. */
  preferences: 'vibesys:welcome:preferences-changed',
} as const;

/** One ordered welcome event from the main process. */
export interface WelcomeEvent<T> {
  /** Monotonically increases for every welcome event sent by this window. */
  readonly sequence: number;
  readonly value: T;
}

export interface WelcomeOverview {
  /** Events at or before this cursor are already covered by this overview. */
  readonly eventCursor: number;
  readonly recent: readonly RecentRun[];
  /** The saved runs `recent` leaves out because they could not be read; empty when none. */
  readonly skippedRecent: readonly SkippedRecent[];
  /** The latest value heard from every host watch. */
  readonly listings: readonly HostListing[];
}

/** What directory completion established (`@vibesys/hosts` owns it, with its reducer). */
export type {PathCompletion} from '@vibesys/hosts';

export type WelcomeResult<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly authNeeded: boolean};

/**
 * A bridge call that rejected (the main process went away) as a failed result, so a store waiting
 * on the reply always gets one.
 */
export function settled<T>(call: Promise<WelcomeResult<T>>): Promise<WelcomeResult<T>> {
  return call.catch(
    (error: unknown): WelcomeResult<T> => ({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      authNeeded: false,
    }),
  );
}
