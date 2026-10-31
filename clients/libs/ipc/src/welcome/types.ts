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
  /** Host settings committed; readers reload their own host from the shared store. */
  hostSettings: 'vibesys:welcome:host-settings-changed',
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

/**
 * Why a request failed when the cause is a fact of the connection to the machine, which the main
 * process knows by type: the screens word it as a status and keep `error`, the raw text (paths,
 * exit codes), for a tooltip. A failure with no cause worded itself.
 */
export type FailureCause = 'link' | 'auth' | 'incompatible' | 'run-gone' | 'denied' | 'capacity';

/** A cause as a status word. */
export const FAILURE_CAUSE_WORDS: Readonly<Record<FailureCause, string>> = {
  link: 'Unreachable',
  auth: 'Sign-in needed',
  incompatible: 'Incompatible',
  'run-gone': 'Run gone',
  denied: 'Denied',
  capacity: 'Too many sessions',
};

export type WelcomeResult<T> =
  | {readonly ok: true; readonly value: T}
  | {
      readonly ok: false;
      readonly error: string;
      readonly authNeeded: boolean;
      /** Set when the failure is the connection's, not the request's own words. */
      readonly cause?: FailureCause;
    };

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

let lastOperationToken = 0;

/**
 * A token no earlier request of this view carried, to name an operation the view may cancel
 * (a project listing). The main process checks that it is a positive integer and keys the operation
 * by it, so every screen that names operations draws its tokens here and none collides.
 */
export function nextOperationToken(): number {
  lastOperationToken += 1;
  return lastOperationToken;
}

/** What a screen says of a bridge call that failed for a reason the main process did not report. */
export const UNEXPECTED_FAILURE =
  'Something unexpected happened. Try again; if it keeps happening, restart VibeSys.';

/** A call that did not answer: it threw, or its promise rejected. */
export interface UnexpectedFailure {
  readonly ok: false;
  readonly error: typeof UNEXPECTED_FAILURE;
  readonly authNeeded: false;
  readonly unexpected: true;
}

/**
 * A bridge call as a result a screen may show. The main process's own answer (a value, or a failure
 * it worded) passes through. A call that throws before it returns (a bridge method that is not
 * there) or whose promise rejects (the main process went away) is a bug or an outage, whose raw
 * error is for developers, not users: it becomes an `UnexpectedFailure` saying so in plain words.
 * `settled` is the older form that passes a rejection's message on.
 */
export async function attempt<T>(
  call: () => Promise<WelcomeResult<T>>,
): Promise<WelcomeResult<T> | UnexpectedFailure> {
  try {
    return await call();
  } catch {
    return {ok: false, error: UNEXPECTED_FAILURE, authNeeded: false, unexpected: true};
  }
}
