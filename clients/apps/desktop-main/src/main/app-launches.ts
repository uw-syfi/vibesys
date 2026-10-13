/**
 * The desktop's process-level launch ordering, independent of Electron.
 *
 * The app's own launch is opened first. Launches forwarded by Electron's single-instance lock are
 * retained until the desktop exists, then each focuses and opens on that desktop exactly once, in
 * arrival order. A forwarded launch's failure is isolated from the other launches.
 */

export interface LaunchTarget<Plan> {
  focus(): void;
  open(plan: Plan): Promise<void>;
}

export class AppLaunches<Plan> {
  readonly #secondFailed: (error: Error) => void;
  #target: LaunchTarget<Plan> | null = null;
  #pending: Plan[] = [];
  #state: 'waiting' | 'opening-initial' | 'ready' | 'failed' = 'waiting';
  #draining = false;

  constructor(secondFailed: (error: Error) => void) {
    this.#secondFailed = secondFailed;
  }

  /** Retain or immediately open one launch forwarded by the single-instance lock. */
  second(plan: Plan): void {
    if (this.#state === 'failed') return;
    this.#pending.push(plan);
    if (this.#state === 'ready') void this.#drain();
  }

  /** Install the desktop and open the process's own plan before the retained second launches. */
  async ready(target: LaunchTarget<Plan>, initial: Plan): Promise<void> {
    if (this.#state !== 'waiting') throw new Error('desktop launches are already ready');
    this.#target = target;
    this.#state = 'opening-initial';
    try {
      await target.open(initial);
    } catch (error) {
      this.#state = 'failed';
      this.#pending = [];
      throw error;
    }
    this.#state = 'ready';
    void this.#drain();
  }

  /** Drain every forwarded launch serially; concurrent callers only append to the owned queue. */
  async #drain(): Promise<void> {
    if (this.#draining || this.#state !== 'ready') return;
    const target = this.#target;
    if (target === null) throw new Error('desktop launches are not ready');
    this.#draining = true;
    try {
      while (this.#pending.length > 0) {
        // Length proves the first element exists, including when `undefined` is a valid `Plan`.
        const plan = this.#pending[0] as Plan;
        this.#pending.shift();
        try {
          target.focus();
          await target.open(plan);
        } catch (error) {
          try {
            this.#secondFailed(error instanceof Error ? error : new Error(String(error)));
          } catch {
            // Failure reporting is diagnostic and detached from startup. It cannot own the FIFO
            // drain or strand later launches when the injected reporter itself is broken.
          }
        }
      }
    } finally {
      this.#draining = false;
    }
  }
}
