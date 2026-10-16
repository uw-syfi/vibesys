/**
 * The windows that each show one run (the Run screen's "Open in new window"), by run: opening a
 * run already shown focuses its window; closing a window forgets it and leaves the run alone (a
 * run window holds no run page or binding of its own, only the shell's Run screen, which reads the
 * run through the welcome bridge like the main window does).
 */
import {runWindowUrl, type WelcomeEvent} from '@vibesys/ipc';
import type {RunRef} from '@vibesys/runs';
import type {WelcomeDocument} from './welcome-ipc.js';

/** What the registry needs of one window. */
export interface RunWindow {
  /** The window's trusted surface, which the welcome router admits. */
  readonly welcome: WelcomeDocument;
  focus(): void;
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void;
}

export interface RunWindowsDeps {
  /**
   * A new window whose trusted surface loads `url`, titled `title`; `closed` runs once when the
   * native window closes.
   */
  readonly create: (url: string, title: string, closed: () => void) => RunWindow;
  /** The app shell's page, which shows a run alone when its query names one. */
  readonly shellUrl: string;
}

function key(run: RunRef): string {
  return JSON.stringify([run.host, run.runId]);
}

export class RunWindows {
  readonly #deps: RunWindowsDeps;
  readonly #windows = new Map<string, RunWindow>();

  constructor(deps: RunWindowsDeps) {
    this.#deps = deps;
  }

  /** Show `run` in its window: focus the one showing it, or open one (`created`). */
  open(run: RunRef, title: string): {readonly window: RunWindow; readonly created: boolean} {
    const shown = this.#windows.get(key(run));
    if (shown !== undefined) {
      shown.focus();
      return {window: shown, created: false};
    }
    const window = this.#deps.create(runWindowUrl(this.#deps.shellUrl, run), title, () => {
      if (this.#windows.get(key(run)) === window) this.#windows.delete(key(run));
    });
    this.#windows.set(key(run), window);
    return {window, created: true};
  }

  /** Send one ordered `event` to every run window's surface. */
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void {
    for (const window of this.#windows.values()) window.sendWelcome(channel, event);
  }
}
