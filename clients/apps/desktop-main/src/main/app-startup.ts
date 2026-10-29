/**
 * The Electron-free coordinator for the desktop app's post-ready startup.
 *
 * It owns each resource as soon as construction succeeds. Every startup failure is reported and
 * enters the one process lifecycle, whose release effect calls `release` below.
 */

export interface StartupLifecycle {
  quit(code: number): void;
}

export interface AppStartupDeps<Plan, Manager extends AsyncDisposable, Desktop> {
  readonly ready: () => Promise<void>;
  readonly connectionManager: () => Promise<Manager>;
  readonly openWindow: (manager: Manager) => Promise<Desktop>;
  readonly openInitial: (desktop: Desktop, plan: Plan) => Promise<void>;
  readonly log: (message: string) => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class AppStartup<Plan, Manager extends AsyncDisposable, Desktop> {
  readonly #deps: AppStartupDeps<Plan, Manager, Desktop>;
  #manager: Manager | null = null;
  /** The manager factory already admitted by startup, including a late manager's retirement. */
  #managerAcquisition: Promise<Manager | null> | null = null;
  /** The one disposal of the exact manager this coordinator acquired. */
  #managerDisposal: Promise<void> | null = null;
  /** Non-null is the terminal shutdown intent as well as the task every release caller joins. */
  #releaseTask: Promise<void> | null = null;

  constructor(deps: AppStartupDeps<Plan, Manager, Desktop>) {
    this.#deps = deps;
  }

  /** Start once readiness is granted; every rejected stage quits through `lifecycle`. */
  async start(plan: Plan, lifecycle: StartupLifecycle): Promise<void> {
    try {
      await this.#deps.ready();
      if (this.#releasing) return;
      const manager = await this.#acquireManager();
      if (manager === null || this.#releasing) return;
      const desktop = await this.#deps.openWindow(manager);
      if (this.#releasing) return;
      await this.#deps.openInitial(desktop, plan);
    } catch (error) {
      // A failure from work shutdown already owns (including late manager disposal) belongs to the
      // release task. It must not become a second quit that skips the lifecycle's bounded join.
      if (this.#releasing) return;
      this.#deps.log(errorMessage(error));
      lifecycle.quit(1);
    }
  }

  /** Publish terminal shutdown, join admitted construction, and release its manager exactly once. */
  release(): Promise<void> {
    if (this.#releaseTask !== null) return this.#releaseTask;
    let complete = (): void => {};
    let fail = (_error: unknown): void => {};
    // Publish the terminal marker before cleanup calls external code. Re-entrant release callers
    // therefore join this promise even if manager disposal itself asks the app to quit again.
    this.#releaseTask = new Promise<void>((resolve, reject) => {
      complete = resolve;
      fail = reject;
    });
    void this.#releaseManager().then(complete, fail);
    return this.#releaseTask;
  }

  get #releasing(): boolean {
    return this.#releaseTask !== null;
  }

  /** Admit one factory before it runs, then transfer every manager it creates to this owner. */
  async #acquireManager(): Promise<Manager | null> {
    const acquisition = Promise.resolve()
      .then(() => (this.#releasing ? null : this.#deps.connectionManager()))
      .then(async manager => {
        if (manager === null) return null;
        this.#manager = manager;
        if (!this.#releasing) return manager;
        await this.#disposeManager();
        return null;
      });
    this.#managerAcquisition = acquisition;
    try {
      return await acquisition;
    } finally {
      if (this.#managerAcquisition === acquisition) this.#managerAcquisition = null;
    }
  }

  /** Join a factory already admitted by startup before retiring the resource it produced. */
  #releaseManager(): Promise<void> {
    const acquisition = this.#managerAcquisition;
    return acquisition === null
      ? this.#disposeManager()
      : acquisition.then(() => this.#disposeManager());
  }

  /** Begin or join the one disposal; ownership is retained unless disposal succeeds. */
  #disposeManager(): Promise<void> {
    if (this.#managerDisposal !== null) return this.#managerDisposal;
    const manager = this.#manager;
    if (manager === null) return Promise.resolve();
    this.#managerDisposal = (async () => {
      await manager[Symbol.asyncDispose]();
      if (this.#manager === manager) this.#manager = null;
    })();
    return this.#managerDisposal;
  }
}
