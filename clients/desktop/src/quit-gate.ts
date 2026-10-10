/**
 * Quitting the bundled app: release the hosts (streams, SSH masters) before the process exits.
 *
 * Electron's `will-quit` cannot wait for a promise, so the first one is cancelled while the hosts
 * are released. The process then exits directly: by then every window is closed and the release is
 * done, and a second `app.quit()` from inside the same quit is ignored by Electron, which left the
 * app running with no window. Runs are detached and keep going; quitting ends only this app's
 * links to them. Electron-free: the main process supplies `release` and `exit`.
 */
export interface QuitDeps {
  /** Release every host; its failure does not stop the quit. */
  readonly release: () => Promise<void>;
  /** End the process now, without quit events. */
  readonly exit: (code: number) => void;
}

export class QuitGate {
  readonly #deps: QuitDeps;
  #releasing: Promise<void> | null = null;

  constructor(deps: QuitDeps) {
    this.#deps = deps;
  }

  /** Handle `will-quit`: cancel it, release the hosts, then exit. */
  willQuit(event: {preventDefault(): void}): void {
    event.preventDefault();
    this.exit(0);
  }

  /** Release the hosts, then exit with `code`; later calls wait for the first one. */
  exit(code: number): Promise<void> {
    this.#releasing ??= this.#deps
      .release()
      .catch(() => {})
      .then(() => this.#deps.exit(code));
    return this.#releasing;
  }
}
