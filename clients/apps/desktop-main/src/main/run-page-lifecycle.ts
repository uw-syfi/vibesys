/** Effects by which the Electron shell interprets ownership of one run page. */
export interface RunPageLifecycleEffects<Page> {
  /** Remove and close a page that this owner has fenced. */
  retire(page: Page): void;
  /** Restore the window to its page-independent welcome presentation. */
  showWelcome(): void;
  /** Report that the current page disappeared without its binding closing it. */
  currentPageLost(): void;
}

/** The failure boundary of a run page, independent of Electron's concrete WebContents. */
export interface RunPageContents {
  once(event: 'render-process-gone' | 'destroyed', listener: () => void): unknown;
  loadURL(url: string): Promise<void>;
}

/**
 * Observe every way the run document can disappear, registering both event fences before load.
 * `lost` may be called more than once; its lifecycle owner decides whether this page is current.
 */
export async function observeRunPage(
  contents: RunPageContents,
  url: string,
  lost: () => void,
  loadFailed: () => void,
): Promise<void> {
  contents.once('render-process-gone', lost);
  contents.once('destroyed', lost);
  try {
    await contents.loadURL(url);
  } catch {
    loadFailed();
    lost();
  }
}

/**
 * One-owner slot for the run page.
 *
 * Replacement installs the successor before retiring the predecessor, so a synchronous
 * destruction callback from the old page cannot revoke the new page. Deliberate close and
 * unexpected loss both fence the page before performing effects; only unexpected loss asks the
 * run-control owner to detach its binding.
 */
export class RunPageLifecycle<Page> {
  readonly #effects: RunPageLifecycleEffects<Page>;
  #current: Page | null = null;

  constructor(effects: RunPageLifecycleEffects<Page>) {
    this.#effects = effects;
  }

  get current(): Page | null {
    return this.#current;
  }

  /** Install `page`, retiring the previous page after it has lost authority. */
  replace(page: Page): void {
    const previous = this.#current;
    this.#current = page;
    if (previous !== null && previous !== page) this.#effects.retire(previous);
  }

  /** Deliberately close the current page, optionally proving which page requested it. */
  close(page: Page | null = this.#current): boolean {
    if (page === null || this.#current !== page) return false;
    this.#leave(page, false);
    return true;
  }

  /** Retire a current page that failed independently of its run-control binding. */
  lost(page: Page): boolean {
    if (this.#current !== page) return false;
    this.#leave(page, true);
    return true;
  }

  /** Fence and retire the current page while the containing native window is closing. */
  abandon(): void {
    const page = this.#current;
    this.#current = null;
    if (page !== null) this.#effects.retire(page);
  }

  /** Fence `page`, attempt every ordered terminal effect, then preserve the first failure. */
  #leave(page: Page, unexpected: boolean): void {
    this.#current = null;
    const failures: Array<{readonly error: unknown}> = [];
    const attempt = (effect: () => void): void => {
      try {
        effect();
      } catch (error) {
        failures.push({error});
      }
    };
    attempt(() => this.#effects.retire(page));
    attempt(() => this.#effects.showWelcome());
    if (unexpected) attempt(() => this.#effects.currentPageLost());
    const firstFailure = failures[0];
    if (firstFailure !== undefined) throw firstFailure.error;
  }
}
