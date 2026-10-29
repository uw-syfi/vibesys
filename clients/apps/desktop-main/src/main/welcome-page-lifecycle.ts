/** Why the current welcome document stopped being usable. */
export type WelcomePageLoss = 'load-failed' | 'render-process-gone' | 'destroyed';

/** What the welcome-page owner did with a loss report. */
export type WelcomePageLossOutcome = 'ignored' | 'recovered' | 'fatal';

/** Effects by which the Electron shell owns one replaceable welcome page. */
export interface WelcomePageLifecycleEffects<Page> {
  /** Construct a replacement page. */
  readonly recreate: () => Page;
  /** Install and begin observing a page; `lost` remains fenced to this exact page. */
  readonly activate: (page: Page, lost: (reason: WelcomePageLoss) => void) => void;
  /** Remove and close a page after it has lost authority. */
  readonly retire: (page: Page) => void;
  /** Revoke the retiring document, before a successor can issue work or fatal shutdown begins. */
  readonly documentRetired: () => void;
  /** Record a recoverable loss. */
  readonly recovering: (reason: WelcomePageLoss) => void;
  /** Enter the app's fatal shutdown path after the one recovery is exhausted or cannot start. */
  readonly fatal: (reason: WelcomePageLoss | 'recreation-failed') => void;
}

/** The Electron surface needed to observe one welcome document. */
export interface WelcomePageContents {
  once(event: 'render-process-gone' | 'destroyed', listener: () => void): unknown;
  loadURL(url: string): Promise<void>;
}

/**
 * Observe loss before starting the document load. Synchronous and asynchronous load failures enter
 * the same typed path as renderer loss; duplicate reports are fenced by `WelcomePageLifecycle`.
 */
export function observeWelcomePage(
  contents: WelcomePageContents,
  url: string,
  lost: (reason: WelcomePageLoss) => void,
): void {
  contents.once('render-process-gone', () => lost('render-process-gone'));
  contents.once('destroyed', () => lost('destroyed'));
  let loading: Promise<void>;
  try {
    loading = contents.loadURL(url);
  } catch {
    lost('load-failed');
    return;
  }
  void loading.catch(() => lost('load-failed'));
}

/**
 * One-owner slot for the long-lived welcome page.
 *
 * The initial page gets one replacement over the native window's lifetime. A loss fences and
 * retires that exact page before revoking its document and activating the successor. Loss of the
 * successor, or failure to construct or activate it, enters fatal shutdown exactly once. Native
 * window abandonment is ordinary teardown and never spends recovery or reports fatal failure.
 */
export class WelcomePageLifecycle<Page> {
  readonly #effects: WelcomePageLifecycleEffects<Page>;
  #current: Page | null = null;
  #started = false;
  #recovered = false;
  #terminal = false;

  constructor(effects: WelcomePageLifecycleEffects<Page>) {
    this.#effects = effects;
  }

  get current(): Page | null {
    return this.#current;
  }

  /** Install and observe the initial page exactly once. */
  start(page: Page): void {
    if (this.#started) throw new Error('the welcome page lifecycle is already started');
    this.#started = true;
    this.#current = page;
    try {
      this.#effects.activate(page, reason => this.lost(page, reason));
    } catch (error) {
      // A synchronous loss may already have recovered (or failed) re-entrantly. That transition
      // owns the page now; do not retire it or report its fatal outcome a second time.
      if (this.#current !== page) return;
      this.#current = null;
      this.#terminal = true;
      try {
        this.#effects.retire(page);
      } catch {
        // Startup owns the original activation failure and the process-level fatal transition.
      }
      try {
        this.#effects.documentRetired();
      } catch {
        // Preserve the activation failure that AppStartup reports and uses to begin shutdown.
      }
      throw error;
    }
  }

  /** Report loss of `page`, recovering once and fencing every stale or duplicate callback. */
  lost(page: Page, reason: WelcomePageLoss): WelcomePageLossOutcome {
    if (this.#terminal || this.#current !== page) return 'ignored';
    this.#current = null;
    let retirementFailed = false;
    try {
      this.#effects.retire(page);
    } catch {
      retirementFailed = true;
    }
    try {
      this.#effects.documentRetired();
    } catch {
      return this.#fail('recreation-failed');
    }
    if (retirementFailed) return this.#fail('recreation-failed');
    if (this.#recovered) return this.#fail(reason);
    this.#recovered = true;
    let replacement: Page | null = null;
    try {
      this.#effects.recovering(reason);
      const successor = this.#effects.recreate();
      replacement = successor;
      this.#current = successor;
      this.#effects.activate(successor, nextReason => this.lost(successor, nextReason));
      // Activation may report a synchronous load failure and exhaust recovery re-entrantly.
      return this.#current === successor ? 'recovered' : 'fatal';
    } catch {
      // Activation may have reported a synchronous loss before it threw. That nested
      // transition already fenced and retired the successor, so only the frame that
      // still owns the exact replacement may release it here.
      if (replacement !== null && this.#current === replacement) {
        this.#current = null;
        try {
          this.#effects.retire(replacement);
        } catch {
          // Fatal shutdown owns final cleanup; preserve the activation failure as the root cause.
        }
      }
      return this.#fail('recreation-failed');
    }
  }

  /** Fence and retire the current page while its containing native window closes. */
  abandon(): void {
    if (this.#terminal) return;
    this.#terminal = true;
    const page = this.#current;
    this.#current = null;
    if (page !== null) this.#effects.retire(page);
  }

  #fail(reason: WelcomePageLoss | 'recreation-failed'): 'fatal' {
    if (this.#terminal) return 'fatal';
    this.#terminal = true;
    this.#current = null;
    this.#effects.fatal(reason);
    return 'fatal';
  }
}
