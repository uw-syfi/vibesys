import type {ScheduleTimeout} from '../index.js';

interface ScheduledEntry {
  readonly callback: () => void;
  readonly delayMs: number;
  readonly dueAt: number;
  cancelled: boolean;
}

/**
 * Package-owned deterministic timer Fake.
 *
 * It structurally satisfies both `ScheduleTimeout` and the Node client's
 * `ClientClock`. Tests that only need scheduling use `schedule`; deadline
 * tests also read and advance `now`. The helper is excluded from production
 * builds by name but included by the check configuration through its imports.
 */
export class FakeClock {
  #nowMs = 0;
  readonly #entries: ScheduledEntry[] = [];
  #scheduled: Array<() => void> = [];

  now(): number {
    return this.#nowMs;
  }

  readonly schedule: ScheduleTimeout = (callback, delayMs = 0) => {
    const entry = {callback, delayMs, dueAt: this.#nowMs + delayMs, cancelled: false};
    this.#entries.push(entry);
    for (const notify of this.#scheduled.splice(0)) notify();
    return () => {
      entry.cancelled = true;
    };
  };

  readonly scheduleTimeout: ScheduleTimeout = this.schedule;

  /** Fire all timers that were pending when this operation began. */
  runPending(): void {
    for (const entry of this.#entries.splice(0)) {
      if (!entry.cancelled) entry.callback();
    }
  }

  /** Fire only timers whose original delay is within `maxDelayMs`. */
  runDue(maxDelayMs: number): void {
    const due = this.#entries.filter(entry => entry.delayMs <= maxDelayMs);
    for (const entry of due) {
      this.#entries.splice(this.#entries.indexOf(entry), 1);
      if (!entry.cancelled) entry.callback();
    }
  }

  /** Fire the first live timer, if one is pending. */
  runOne(): void {
    while (this.#entries.length > 0) {
      const entry = this.#entries.shift();
      if (entry !== undefined && !entry.cancelled) {
        this.#nowMs = Math.max(this.#nowMs, entry.dueAt);
        entry.callback();
        return;
      }
    }
  }

  /** Run the next live timer with this delay, waiting until production arms it. */
  async runNext(delayMs: number): Promise<void> {
    const entry = await this.waitUntilScheduled(delayMs);
    const index = this.#entries.indexOf(entry);
    if (index < 0 || entry.cancelled) return;
    this.#entries.splice(index, 1);
    this.#nowMs = Math.max(this.#nowMs, entry.dueAt);
    entry.callback();
  }

  /** Resolve with the next live timer of this duration without firing it. */
  async waitUntilScheduled(delayMs: number): Promise<ScheduledEntry> {
    let entry = this.#next(delayMs);
    while (entry === undefined) {
      await new Promise<void>(resolve => this.#scheduled.push(resolve));
      entry = this.#next(delayMs);
    }
    return entry;
  }

  /** Advance virtual time and fire every timer due in the interval. */
  advanceBy(elapsedMs: number): void {
    const target = this.#nowMs + elapsedMs;
    while (true) {
      const entry = this.#entries
        .filter(candidate => !candidate.cancelled && candidate.dueAt <= target)
        .sort((left, right) => left.dueAt - right.dueAt)[0];
      if (entry === undefined) break;
      this.#entries.splice(this.#entries.indexOf(entry), 1);
      this.#nowMs = entry.dueAt;
      entry.callback();
    }
    this.#nowMs = target;
  }

  pendingDelays(): number[] {
    return this.#entries
      .filter(entry => !entry.cancelled)
      .map(entry => entry.dueAt - this.#nowMs)
      .sort((left, right) => left - right);
  }

  #next(delayMs: number): ScheduledEntry | undefined {
    return this.#entries.find(entry => !entry.cancelled && entry.delayMs === delayMs);
  }
}
