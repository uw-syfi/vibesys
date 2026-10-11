/**
 * The shared reconnect-backoff policy. One schedule governs every redial loop
 * in the client stack: the subscription (`PersistentEventStream`), the control
 * channel (`ServerClient`), and a resume-after-sleep watcher (#832). Keeping it
 * here, rather than as a constant copied per call site, means a change to the
 * cadence is one edit, not a hunt for duplicates that have already drifted.
 */

/**
 * Delay before each reconnect attempt after a drop, in order. The schedule is
 * finite by design: a peer that refuses this many dials in a row is not coming
 * back on its own, so the loop stops and leaves the disconnect standing as the
 * answer rather than redialing forever. A successful reconnect resets the
 * position, so the next outage gets the whole schedule again.
 */
export const DEFAULT_RECONNECT_DELAYS_MS: readonly number[] = [500, 1_000, 2_000, 4_000, 8_000];

/**
 * A cursor over a finite backoff schedule. Each `next()` yields the following
 * delay and advances; once the schedule is spent it yields `undefined`, which
 * is the caller's signal to stop redialing. `reset()` returns to the start so a
 * recovered connection's next outage backs off from the beginning.
 *
 * It owns only the position, not the timer: the caller decides when to sleep
 * and when to dial, so the same policy drives an event-loop `setTimeout` loop
 * and a manual retry affordance without either reaching into the other's state.
 */
export class BackoffSchedule {
  readonly #delaysMs: readonly number[];
  #attempt = 0;

  constructor(delaysMs: readonly number[] = DEFAULT_RECONNECT_DELAYS_MS) {
    this.#delaysMs = delaysMs;
  }

  /** The next delay in ms, or `undefined` once the finite schedule is spent. */
  next(): number | undefined {
    const delay = this.#delaysMs[this.#attempt];
    if (delay === undefined) return undefined;
    this.#attempt += 1;
    return delay;
  }

  /** Whether the schedule is spent, so a further `next()` would yield nothing. */
  get exhausted(): boolean {
    return this.#attempt >= this.#delaysMs.length;
  }

  /** Return to the first delay, so a fresh outage backs off from the start. */
  reset(): void {
    this.#attempt = 0;
  }
}
