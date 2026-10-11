/**
 * What the welcome view and the main process exchange (`ipcRenderer.invoke` channels, and the
 * events the main process sends).
 *
 * The welcome view is a trusted app page in the app's one window, but the main process still treats
 * its requests as input: a host key must parse to a usable host, paths are validated before they
 * reach a host, and no host command line is ever built from these strings. The run view never gets
 * these channels; only the welcome view's own web contents does, by the sender's identity.
 *
 * Host data reaches the view as pushes (`listing`), folded from the host helper's
 * `instances.watch`: there is no refresh request to forget, and no timer.
 */
import type {
  ConnectionStatus,
  HostKey,
  HostListing,
  HostSummary,
  RecentRun,
  ResumeRunId,
  StopView,
} from '@vibesys/entities';

export const WELCOME_CHANNELS = {
  overview: 'vibesys:welcome:overview',
  hosts: 'vibesys:welcome:hosts',
  host: 'vibesys:welcome:host',
  setCheckout: 'vibesys:welcome:set-checkout',
  cancelCheckout: 'vibesys:welcome:cancel-checkout',
  signIn: 'vibesys:welcome:sign-in',
  tasks: 'vibesys:welcome:tasks',
  complete: 'vibesys:welcome:complete',
  attach: 'vibesys:welcome:attach',
  start: 'vibesys:welcome:start',
  cancelStart: 'vibesys:welcome:cancel-start',
  resume: 'vibesys:welcome:resume',
  showRun: 'vibesys:welcome:show-run',
  showWelcome: 'vibesys:welcome:show-welcome',
  retry: 'vibesys:welcome:retry',
  stop: 'vibesys:welcome:stop',
} as const;

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
 * What the welcome preload exposes to the welcome view (`window.vibesysWelcome`): one function per
 * request channel, and one subscription per event. The main process validates every argument.
 */
export interface WelcomeBridge {
  readonly platform: string;
  overview(): Promise<WelcomeResult<WelcomeOverview>>;
  hosts(): Promise<WelcomeResult<string[]>>;
  host(host: HostKey): Promise<WelcomeResult<HostSummary>>;
  /** Change a checkout under `change`, so losing renderer intent can cancel the main-process work. */
  setCheckout(host: HostKey, path: string, change: number): Promise<WelcomeResult<string>>;
  cancelCheckout(change: number): Promise<WelcomeResult<null>>;
  signIn(host: HostKey): Promise<WelcomeResult<null>>;
  tasks(host: HostKey, project: string): Promise<WelcomeResult<string[]>>;
  /** The directories that complete `directory` (a path ending in `/`) on `host`. */
  complete(host: HostKey, directory: string): Promise<WelcomeResult<PathCompletion>>;
  attach(host: HostKey, instance: string): Promise<WelcomeResult<null>>;
  /** Start `task`; `start` names this start so that `cancelStart` can cancel it. */
  start(
    host: HostKey,
    project: string,
    task: string,
    args: string,
    start: number,
  ): Promise<WelcomeResult<StartOutcome>>;
  cancelStart(start: number): Promise<WelcomeResult<null>>;
  resume(host: HostKey, project: string, run: ResumeRunId): Promise<WelcomeResult<null>>;
  showRun(): Promise<WelcomeResult<null>>;
  showWelcome(): Promise<WelcomeResult<null>>;
  retry(): Promise<WelcomeResult<null>>;
  stop(host: HostKey, instance: string, force: boolean): Promise<WelcomeResult<null>>;
  onChrome(listener: (event: WelcomeEvent<ChromeState>) => void): void;
  onRecent(listener: (event: WelcomeEvent<readonly RecentRun[]>) => void): void;
  onShowHost(listener: (event: WelcomeEvent<HostKey>) => void): void;
  onListing(listener: (event: WelcomeEvent<HostListing>) => void): void;
}
