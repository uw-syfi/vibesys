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

interface DesignQueryOwnerConfig<T> {
  readonly request: () => Promise<T>;
  readonly publish: (value: T) => void;
  readonly report: (error: unknown) => void;
}

/** Serializes the design pane and supplemental design-log query consumers. */
export class DesignQueryOwner<T> {
  #active: QueryCycle | null = null;
  #allowed = true;
  #generation = 0;
  #paneFailureWanted = false;

  constructor(private readonly config: DesignQueryOwnerConfig<T>) {}

  async refresh(reportPaneFailure = false): Promise<void> {
    if (!this.#allowed) return;
    if (reportPaneFailure) this.#paneFailureWanted = true;
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
    if (this.#active !== null) this.#active.refreshPending = false;
    this.#paneFailureWanted = false;
  }

  async #run(cycle: QueryCycle): Promise<void> {
    while (this.#allowed) {
      cycle.refreshPending = false;
      const outcome = await this.#request(++this.#generation);
      if (!this.#allowed) return;
      if (cycle.refreshPending) continue;

      // Retire before publishing. A state listener that synchronously asks for
      // another refresh starts a new cycle instead of losing its request in a
      // cycle that has already selected its authoritative outcome.
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
    if (outcome.kind === 'success') {
      // Clear this cycle's intent before publication. A synchronous state
      // listener may start another pane cycle, whose failure ownership must
      // survive this cycle's completion.
      this.#paneFailureWanted = false;
      this.config.publish(outcome.value);
      return;
    }
    const report = this.#paneFailureWanted;
    this.#paneFailureWanted = false;
    if (report && this.#allowed) this.config.report(outcome.error);
  }
}
