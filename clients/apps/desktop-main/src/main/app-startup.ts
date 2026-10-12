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

  constructor(deps: AppStartupDeps<Plan, Manager, Desktop>) {
    this.#deps = deps;
  }

  /** Start once readiness is granted; every rejected stage quits through `lifecycle`. */
  async start(plan: Plan, lifecycle: StartupLifecycle): Promise<void> {
    try {
      await this.#deps.ready();
      const manager = await this.#deps.connectionManager();
      this.#manager = manager;
      const desktop = await this.#deps.openWindow(manager);
      await this.#deps.openInitial(desktop, plan);
    } catch (error) {
      this.#deps.log(errorMessage(error));
      lifecycle.quit(1);
    }
  }

  /** Release the constructed manager at most once through the app lifecycle. */
  async release(): Promise<void> {
    const manager = this.#manager;
    this.#manager = null;
    await manager?.[Symbol.asyncDispose]();
  }
}
