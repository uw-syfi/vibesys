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
 * A second launch of the app is refused by the single-instance lock (`main.ts`); the first instance
 * focuses its window and opens what the second was asked to open (`secondLaunch`).
 *
 * Electron-free: the main process supplies `release`, `exit`, and a timer.
 */
import type {ScheduleTimeout} from '@vibesys/backend-client';

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
        void this.#deps
          .release()
          .catch(() => {})
          .then(() => this.#dispatch({type: 'released'}));
        return;
      case 'start-bound':
        this.#cancelBound = this.#deps.scheduleTimeout(
          () => this.#dispatch({type: 'bound-elapsed'}),
          this.#deps.releaseBoundMs,
        );
        return;
      case 'exit':
        this.#cancelBound();
        this.#deps.exit(effect.code);
        return;
    }
  }
}
