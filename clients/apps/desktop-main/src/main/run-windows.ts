/**
 * The windows that each show one run (the Run screen's "Open in new window"), by run: opening a
 * run already shown focuses its window, and a run with a window of its own is live there (its
 * attach binds that window's surface, not the main window's). Closing a run window forgets it and
 * detaches its live connection; the run itself goes on.
 */
import {runWindowUrl, type WelcomeEvent} from '@vibesys/ipc';
import {type RunRef, runRefKey} from '@vibesys/runs';
import type {WelcomeDocument} from './welcome-ipc.js';

/** What the registry needs of one window. */
export interface RunWindow {
  /** The window's trusted surface, which the welcome router admits. */
  readonly welcome: WelcomeDocument;
  focus(): void;
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void;
}

/** A window the registry holds for one composite run identity. */
interface Shown<W> {
  readonly window: W;
}

export interface RunWindowsDeps<W extends RunWindow> {
  /**
   * A new window whose trusted surface loads `url`, titled `title`; `closed` runs once when the
   * native window closes.
   */
  readonly create: (url: string, title: string, closed: () => void) => W;
  /** The app shell's page, which shows a run alone when its query names one. */
  readonly shellUrl: string;
}

export class RunWindows<W extends RunWindow = RunWindow> {
  readonly #deps: RunWindowsDeps<W>;
  readonly #windows = new Map<string, Shown<W>>();

  constructor(deps: RunWindowsDeps<W>) {
    this.#deps = deps;
  }

  /** Show `run` in its window: focus the one showing it, or open one (`created`). */
  open(run: RunRef, title: string): {readonly window: W; readonly created: boolean} {
    const key = runRefKey(run);
    const shown = this.#windows.get(key);
    if (shown !== undefined) {
      shown.window.focus();
      return {window: shown.window, created: false};
    }
    const window = this.#deps.create(runWindowUrl(this.#deps.shellUrl, run), title, () => {
      if (this.#windows.get(key)?.window === window) this.#windows.delete(key);
    });
    this.#windows.set(key, {window});
    return {window, created: true};
  }

  /** The window showing exactly `run`, where that run is live; none for the main window. */
  showing(run: RunRef): W | undefined {
    return this.#windows.get(runRefKey(run))?.window;
  }

  /** Send one ordered `event` to every run window's surface. */
  sendWelcome<T>(channel: string, event: WelcomeEvent<T>): void {
    for (const shown of this.#windows.values()) shown.window.sendWelcome(channel, event);
  }
}
