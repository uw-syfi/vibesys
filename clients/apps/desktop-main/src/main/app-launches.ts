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

  constructor(secondFailed: (error: Error) => void) {
    this.#secondFailed = secondFailed;
  }

  /** Retain or immediately open one launch forwarded by the single-instance lock. */
  second(plan: Plan): void {
    if (this.#target === null) {
      this.#pending.push(plan);
      return;
    }
    this.#openSecond(plan);
  }

  /** Install the desktop and open the process's own plan before the retained second launches. */
  ready(target: LaunchTarget<Plan>, initial: Plan): Promise<void> {
    if (this.#target !== null) throw new Error('desktop launches are already ready');
    this.#target = target;
    const opening = target.open(initial);
    const pending = this.#pending;
    this.#pending = [];
    for (const plan of pending) this.#openSecond(plan);
    return opening;
  }

  #openSecond(plan: Plan): void {
    const target = this.#target;
    if (target === null) throw new Error('desktop launches are not ready');
    target.focus();
    void target.open(plan).catch(error => this.#secondFailed(error as Error));
  }
}
