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
import type {ConnectionStatus, HostKey, HostListing} from '@vibesys/hosts';
import type {RecentRun, StopView} from '@vibesys/runs';

/** The events the main process sends the welcome view. */
export const WELCOME_EVENTS = {
  /** The window's state changed (`ChromeState`). */
  chrome: 'vibesys:welcome:chrome',
  /** The recent runs changed (`RecentRun[]`): the settings store wrote them. */
  recent: 'vibesys:welcome:recent',
  /** Show a host's panel (`HostKey`): a launch or a second launch asked for it. */
  showHost: 'vibesys:welcome:show-host',
  /** A host's runs changed (`HostListing`), as its helper pushed them. */
  listing: 'vibesys:welcome:listing',
} as const;

/** One ordered welcome event from the main process. */
export interface WelcomeEvent<T> {
  /** Monotonically increases for every welcome event sent by this window. */
  readonly sequence: number;
  readonly value: T;
}

/** What the window shows, for the welcome view's title strip. */
export interface ChromeState {
  /** `welcome`: the welcome view fills the window; `run`: the run view does, under the strip. */
  readonly mode: 'welcome' | 'run';
  /** The attached run, when there is one. */
  readonly attached: {
    readonly host: HostKey;
    readonly hostLabel: string;
    /** The registry id of the server driving the run. */
    readonly instanceId: string;
    /** The run's id, from its record. */
    readonly runId: string;
    readonly project: string;
    /** The connection to the run; the strip words it. */
    readonly connection: ConnectionStatus;
  } | null;
  /** The stop flows past idle, by `stopKey` (host, newline, instance id). */
  readonly stops: Readonly<Record<string, StopView>>;
}

export interface WelcomeOverview {
  /** Events at or before this cursor are already covered by this overview. */
  readonly eventCursor: number;
  /** Why the app's settings file was ignored, when it was. */
  readonly settingsProblem: string | null;
  readonly recent: readonly RecentRun[];
  /** The latest value heard from every host watch. */
  readonly listings: readonly HostListing[];
  readonly chrome: ChromeState;
  /** The host whose panel to show first (a launch may name one). */
  readonly host: HostKey;
}

/** Directories that complete a typed path on a host (`paths.complete`). */
export interface PathCompletion {
  /** The directory that was listed. */
  readonly directory: string;
  readonly entries: readonly {
    readonly name: string;
    readonly path: string;
    readonly checkout: boolean;
    readonly project: boolean;
  }[];
  readonly truncated: boolean;
}

/** How a start the welcome view asked for ended: attached, or cancelled (and its run stopped). */
export type StartOutcome = 'started' | 'cancelled';

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
