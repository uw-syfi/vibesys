import type {ScheduleTimeout} from '../index.js';

interface ScheduledEntry {
  readonly callback: () => void;
  readonly dueAt: number;
  readonly order: number;
  cancelled: boolean;
}

/**
 * The one deterministic timer Fake for every client package, exported through
 * `@vibesys/backend-client/testing`.
 *
 * It behaves like `setTimeout` over a virtual `now()`: a timer is due at the
 * time it was scheduled plus its delay, and timers fire earliest-due first,
 * ties in scheduling order. Firing a timer moves `now()` to its due time.
 * It structurally satisfies both `ScheduleTimeout` and the Node client's
 * `ClientClock`. `advanceBy` fires every timer that comes due in the interval,
 * so one jump (hours, to model sleep) fires everything that came due, as
 * Electron does on wake.
 */
export class FakeClock {
  #nowMs = 0;
  #order = 0;
  readonly #entries: ScheduledEntry[] = [];
  #scheduled: Array<() => void> = [];

  now(): number {
    return this.#nowMs;
  }

  readonly schedule: ScheduleTimeout = (callback, delayMs = 0) => {
    const entry = {callback, dueAt: this.#nowMs + delayMs, order: this.#order, cancelled: false};
    this.#order += 1;
    this.#entries.push(entry);
    for (const notify of this.#scheduled.splice(0)) notify();
    return () => {
      entry.cancelled = true;
    };
  };

  readonly scheduleTimeout: ScheduleTimeout = this.schedule;

  /**
   * Move `now()` to the earliest-due live timer and fire it. Returns whether a
   * timer fired.
   */
  runNext(): boolean {
    const entry = this.#earliest(Number.POSITIVE_INFINITY);
    if (entry === undefined) return false;
    this.#fire(entry);
    return true;
  }

  /** Advance virtual time by `elapsedMs`, firing every timer due in the interval. */
  advanceBy(elapsedMs: number): void {
    const target = this.#nowMs + elapsedMs;
    for (let entry = this.#earliest(target); entry !== undefined; entry = this.#earliest(target)) {
      this.#fire(entry);
    }
    this.#nowMs = target;
  }

  /**
   * Advance until every timer live now has come due, firing each in due order
   * (and any timer they schedule that comes due no later).
   */
  runPending(): void {
    const delays = this.pendingDelays();
    const latest = delays[delays.length - 1];
    if (latest !== undefined) this.advanceBy(latest);
  }

  /**
   * Resolve once a live timer is due `delayMs` from now, without firing it.
   * For production code that arms its timers after awaiting real I/O.
   */
  async waitUntilScheduled(delayMs: number): Promise<void> {
    while (
      !this.#entries.some(entry => this.#isLive(entry) && entry.dueAt - this.#nowMs === delayMs)
    ) {
      await new Promise<void>(resolve => this.#scheduled.push(resolve));
    }
  }

  /** Timers scheduled and not yet fired or cancelled. */
  get pending(): number {
    return this.#entries.filter(entry => this.#isLive(entry)).length;
  }

  /** The time left until each live timer is due, smallest first. */
  pendingDelays(): number[] {
    return this.#entries
      .filter(entry => this.#isLive(entry))
      .map(entry => entry.dueAt - this.#nowMs)
      .sort((left, right) => left - right);
  }

  #isLive(entry: ScheduledEntry): boolean {
    return !entry.cancelled;
  }

  #earliest(notAfter: number): ScheduledEntry | undefined {
    let earliest: ScheduledEntry | undefined;
    for (const entry of this.#entries) {
      if (!this.#isLive(entry) || entry.dueAt > notAfter) continue;
      if (
        earliest === undefined ||
        entry.dueAt < earliest.dueAt ||
        (entry.dueAt === earliest.dueAt && entry.order < earliest.order)
      ) {
        earliest = entry;
      }
    }
    return earliest;
  }

  #fire(entry: ScheduledEntry): void {
    this.#entries.splice(this.#entries.indexOf(entry), 1);
    this.#nowMs = Math.max(this.#nowMs, entry.dueAt);
    entry.callback();
  }
}
