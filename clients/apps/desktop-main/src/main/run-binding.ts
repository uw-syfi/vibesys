/** Effects of a run losing its binding without the holder of its page handle acting. */
export interface RunBindingEffects {
  /** `runId` is no longer bound; release whatever it held. */
  detached(runId: string): void;
}

/**
 * One-owner slot for which run is currently bound.
 *
 * `rebind` takes the new run before reporting the old one detached, so a `detached` effect that
 * reaches back into whatever released the old run's page (which may itself read `current`, or
 * call `release` with the old run's id) sees the new run already bound, never the one it replaced.
 */
export class RunBinding {
  readonly #effects: RunBindingEffects;
  #current: string | null = null;

  constructor(effects: RunBindingEffects) {
    this.#effects = effects;
  }

  get current(): string | null {
    return this.#current;
  }

  /** Bind `runId`, then report whatever was bound before (if anything) detached. */
  rebind(runId: string): void {
    const previous = this.#current;
    this.#current = runId;
    if (previous !== null) this.#effects.detached(previous);
  }

  /** Detach whatever is bound and report it; safe when nothing is bound. */
  detachCurrent(): void {
    const runId = this.#current;
    if (runId === null) return;
    this.#current = null;
    this.#effects.detached(runId);
  }

  /** Drop `runId` without reporting it, for a holder that already knows; a stale id does nothing. */
  release(runId: string): void {
    if (this.#current === runId) this.#current = null;
  }
}
