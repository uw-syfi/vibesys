type QueryOutcome<T> =
  | {readonly kind: 'success'; readonly value: T; readonly generation: number}
  | {readonly kind: 'failure'; readonly error: unknown; readonly generation: number};

interface QueryCycle {
  readonly completion: Promise<void>;
  readonly resolve: () => void;
  readonly reject: (error: unknown) => void;
  refreshPending: boolean;
}

function queryCycle(): QueryCycle {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const completion = new Promise<void>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return {completion, resolve, reject, refreshPending: false};
}

export interface LatestQueryOwnerConfig<T> {
  readonly request: () => Promise<T>;
  readonly publish: (value: T) => void;
  readonly report: (error: unknown) => void;
}

/**
 * Own one latest-wins query cycle for the not-yet-migrated TUI controller.
 *
 * A refresh received in flight supersedes that request's success or failure and retains exactly
 * one follow-up. Every waiter shares a completion that settles only after the authoritative
 * follow-up settles. `stop` retires every outcome already in flight.
 */
export class LatestQueryOwner<T> {
  #active: QueryCycle | null = null;
  #allowed = true;
  #generation = 0;

  constructor(private readonly config: LatestQueryOwnerConfig<T>) {}

  async refresh(): Promise<void> {
    if (!this.#allowed) return;
    if (this.#active !== null) {
      this.#active.refreshPending = true;
      await this.#active.completion;
      return;
    }

    const cycle = queryCycle();
    this.#active = cycle;
    void this.#run(cycle).then(cycle.resolve, cycle.reject);
    await cycle.completion;
  }

  stop(): void {
    this.#allowed = false;
    this.#generation += 1;
    if (this.#active !== null) this.#active.refreshPending = false;
  }

  async #run(cycle: QueryCycle): Promise<void> {
    while (this.#allowed) {
      cycle.refreshPending = false;
      const outcome = await this.#request(++this.#generation);
      if (!this.#allowed) return;
      if (cycle.refreshPending) continue;

      if (this.#active === cycle) this.#active = null;
      this.#settle(outcome);
      return;
    }
  }

  async #request(generation: number): Promise<QueryOutcome<T>> {
    try {
      const value = await this.config.request();
      return {kind: 'success', value, generation};
    } catch (error) {
      return {kind: 'failure', error, generation};
    }
  }

  #settle(outcome: QueryOutcome<T>): void {
    if (outcome.generation !== this.#generation) return;
    if (outcome.kind === 'success') this.config.publish(outcome.value);
    else this.config.report(outcome.error);
  }
}
