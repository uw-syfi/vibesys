/**
 * What the welcome view and the main process exchange (`ipcRenderer.invoke` channels, and one
 * event the main process sends).
 *
 * The welcome view is a trusted app page in the app's one window, but the main process still treats
 * its requests as input: a host key must parse to a usable host, paths are validated before they
 * reach a host, and no ssh command line is ever built from these strings directly. The run view
 * never gets these channels; only the welcome view's own web contents does, by the sender's
 * identity.
 */
import type {HostKey} from './host-settings.js';
import type {StopView} from './stop-run.js';

export const WELCOME_CHANNELS = {
  overview: 'vibesys:welcome:overview',
  recentStatus: 'vibesys:welcome:recent-status',
  hosts: 'vibesys:welcome:hosts',
  host: 'vibesys:welcome:host',
  setCheckout: 'vibesys:welcome:set-checkout',
  signIn: 'vibesys:welcome:sign-in',
  tasks: 'vibesys:welcome:tasks',
  attach: 'vibesys:welcome:attach',
  start: 'vibesys:welcome:start',
  resume: 'vibesys:welcome:resume',
  showRun: 'vibesys:welcome:show-run',
  showWelcome: 'vibesys:welcome:show-welcome',
  retry: 'vibesys:welcome:retry',
  stop: 'vibesys:welcome:stop',
} as const;

/** The one event the main process sends the welcome view: the window's state changed. */
export const CHROME_CHANNEL = 'vibesys:welcome:chrome';

/** What the window shows, for the welcome view's title strip. */
export interface ChromeState {
  /** `welcome`: the welcome view fills the window; `run`: the run view does, under the strip. */
  readonly mode: 'welcome' | 'run';
  /** The attached run, when there is one. */
  readonly attached: {
    readonly host: HostKey;
    readonly hostLabel: string;
    /** The registry id of the attached run; null when attached by socket path (it cannot be stopped). */
    readonly instanceId: string | null;
    readonly runId: string | null;
    readonly project: string;
    /** A short status (`connected`, `reconnecting (attempt 2)`, ...). */
    readonly status: string;
    readonly detail: string;
    /** True when the connection stopped trying: offer Retry, unless the run ended. */
    readonly stuck: boolean;
    /** True when the run is no longer running: offer Resume, not Retry. */
    readonly ended: boolean;
  } | null;
  /** The stop flows past idle, by `stopKey` (host, newline, instance id). */
  readonly stops: Readonly<Record<string, StopView>>;
}

/** A recent run as the welcome view lists it. */
export interface WelcomeRecent {
  readonly host: HostKey;
  readonly hostLabel: string;
  readonly project: string;
  readonly task: string | null;
  readonly instanceId: string;
  readonly runId: string | null;
  readonly attachedAt: number;
}

export interface WelcomeOverview {
  /** Why the app's settings file was ignored, when it was. */
  readonly settingsProblem: string | null;
  readonly recent: readonly WelcomeRecent[];
  readonly chrome: ChromeState;
}

/** A recent run's refreshed status (`recent.ts`'s `RecentStatus`). */
export type WelcomeRecentStatus =
  | {readonly kind: 'live'; readonly instanceId: string; readonly status: string}
  | {readonly kind: 'ended'}
  | {readonly kind: 'unknown'; readonly detail: string};

export interface WelcomeRun {
  readonly id: string;
  readonly projectRoot: string;
  readonly runId: string | null;
  readonly status: string;
  readonly startedAt: number;
  /** Null when the app can attach; otherwise why not (a version mismatch). */
  readonly blocked: string | null;
}

/** One host as the welcome view shows it. */
export interface WelcomeHost {
  readonly key: HostKey;
  readonly label: string;
  /** The saved checkout (This Mac: the app's own checkout by default); null until set. */
  readonly checkout: string | null;
  /** A checkout to pre-fill, from the host's live records; null when none says. */
  readonly suggestedCheckout: string | null;
  /** The live runs; empty until a checkout is set. */
  readonly runs: readonly WelcomeRun[];
  /** Project directories to offer when starting a run. */
  readonly projects: readonly string[];
}

export type WelcomeResult<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly authNeeded: boolean};
