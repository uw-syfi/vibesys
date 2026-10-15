/**
 * The one owner of the app's process-wide lifecycle: the quit sequence and the second launch.
 *
 * Quitting is a pure machine, `running -> releasing -> exited`:
 *
 * - The first quit request (Cmd+Q, the last window closing, SIGINT/SIGTERM, a failed launch) starts
 *   one bounded release of the hosts (streams and SSH masters; detached runs keep going).
 * - The release finishing, its bound elapsing, or a second quit request exits. `exited` is
 *   absorbing: the process exits exactly once, through `AppLifecycle`'s `exit`, the app's one exit
 *   call site.
 *
 * Electron's `will-quit` cannot wait for a promise, so the shell cancels it while releasing and
 * then exits directly: a second `app.quit()` from inside the same quit is ignored by Electron, which
 * once left the app running with no window.
 *
 * A second launch of the app is refused by the single-instance lock (`index.ts`); the first instance
 * focuses its window and opens what the second was asked to open (`secondLaunch`).
 *
 * Electron-free: the main process supplies `release`, `exit`, and a timer.
 */
import type {ScheduleTimeout} from '@vibesys/transport';

export type QuitState =
  | {readonly kind: 'running'}
  | {readonly kind: 'releasing'; readonly code: number}
  | {readonly kind: 'exited'; readonly code: number};

export type QuitEvent =
  /** Someone asked to quit, with the exit status to end with. */
  | {readonly type: 'quit'; readonly code: number}
  /** The hosts are released (or their release failed: it does not stop the quit). */
  | {readonly type: 'released'}
  /** The release took longer than its bound. */
  | {readonly type: 'bound-elapsed'};

export type QuitEffect =
  | {readonly kind: 'release'}
  | {readonly kind: 'start-bound'}
  | {readonly kind: 'exit'; readonly code: number};

export const RUNNING: QuitState = {kind: 'running'};

/** The process's own timers, for the shells that take a `ScheduleTimeout`. */
export const realTimers: ScheduleTimeout = (callback, ms) => {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
};

/** The quit machine's step: the next state and the effects the shell performs. */
export function quitStep(
  state: QuitState,
  event: QuitEvent,
): {readonly state: QuitState; readonly effects: readonly QuitEffect[]} {
  switch (state.kind) {
    case 'running':
      return event.type === 'quit'
        ? {
            state: {kind: 'releasing', code: event.code},
            effects: [{kind: 'release'}, {kind: 'start-bound'}],
          }
        : {state, effects: []};
    case 'releasing':
      // The release ending, its bound, or a second request: exit with the first request's code.
      return {
        state: {kind: 'exited', code: state.code},
        effects: [{kind: 'exit', code: state.code}],
      };
    case 'exited':
      return {state, effects: []};
  }
}

export interface AppLifecycleDeps {
  /** Release every host; its failure does not stop the quit. */
  readonly release: () => Promise<void>;
  /** End the process now, without quit events. */
  readonly exit: (code: number) => void;
  readonly scheduleTimeout: ScheduleTimeout;
  /** How long the release may take before the app exits anyway. */
  readonly releaseBoundMs: number;
}

/** The shell over `quitStep`: performs its effects and feeds their results back. */
export class AppLifecycle {
  readonly #deps: AppLifecycleDeps;
  #state: QuitState = RUNNING;
  #cancelBound: () => void = () => {};

  constructor(deps: AppLifecycleDeps) {
    this.#deps = deps;
  }

  get state(): QuitState {
    return this.#state;
  }

  /** Handle Electron's `will-quit`: cancel it, and quit through the machine. */
  willQuit(event: {preventDefault(): void}): void {
    event.preventDefault();
    this.quit(0);
  }

  /** Ask to quit with `code`; later requests while releasing exit at once. */
  quit(code: number): void {
    this.#dispatch({type: 'quit', code});
  }

  #dispatch(event: QuitEvent): void {
    const {state, effects} = quitStep(this.#state, event);
    this.#state = state;
    for (const effect of effects) this.#perform(effect);
  }

  #perform(effect: QuitEffect): void {
    switch (effect.kind) {
      case 'release':
        this.#beginRelease();
        return;
      case 'start-bound':
        this.#startBound();
        return;
      case 'exit':
        this.#exit(effect.code);
        return;
    }
  }

  #beginRelease(): void {
    let release: Promise<void>;
    try {
      release = this.#deps.release();
    } catch {
      this.#dispatch({type: 'released'});
      return;
    }
    void Promise.resolve(release)
      .catch(() => {})
      .then(() => this.#dispatch({type: 'released'}));
  }

  #startBound(): void {
    // A synchronous release failure can exit while the original effect list is still being
    // performed. Do not acquire a timer after that exit.
    if (this.#state.kind !== 'releasing') return;
    let fired = false;
    let acquiring = true;
    const callbackFailures: unknown[] = [];
    let cancel: () => void;
    try {
      cancel = this.#deps.scheduleTimeout(() => {
        fired = true;
        try {
          this.#dispatch({type: 'bound-elapsed'});
        } catch (cause) {
          // Some schedulers may invoke the callback before returning its handle. Capture the
          // callback's failure so acquisition can still publish or retire that handle first.
          if (acquiring) callbackFailures.push(cause);
          else throw cause;
        }
      }, this.#deps.releaseBoundMs);
    } catch (cause) {
      acquiring = false;
      // A scheduler may call back and then fail before returning a handle. Preserve the callback's
      // earlier failure; if the callback completed exit, preserve the scheduler's later failure.
      if (callbackFailures.length > 0) throw callbackFailures[0];
      // Without an acquired bound, waiting for release could strand the process forever.
      if (this.#state.kind === 'releasing') {
        this.#dispatch({type: 'bound-elapsed'});
        return;
      }
      throw cause;
    }
    acquiring = false;
    // A scheduler may call back before returning ownership of its handle. In that case exit
    // could not cancel the not-yet-published handle, so retire it here instead.
    if (fired || this.#state.kind !== 'releasing') this.#cancel(cancel);
    else this.#cancelBound = cancel;
    if (callbackFailures.length > 0) throw callbackFailures[0];
  }

  #exit(code: number): void {
    try {
      this.#cancelBound();
    } catch {
      // A failed cleanup callback must not prevent the process's authoritative exit effect.
    } finally {
      this.#cancelBound = () => {};
      this.#deps.exit(code);
    }
  }

  #cancel(cancel: () => void): void {
    try {
      cancel();
    } catch {
      // The lifecycle is already exited; timer cleanup cannot be allowed to undo that.
    }
  }
}
