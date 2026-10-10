/**
 * A virtual clock for the desktop's timers: `scheduleTimeout` registers, `advance` moves time and
 * fires what came due, in due order. A jump (`advance` by hours) is how a test models sleep: timers
 * that came due while the machine slept fire at once when it wakes, as they do in Electron.
 */
import type {ScheduleTimeout} from '@vibesys/backend-client';

interface Entry {
  readonly dueAt: number;
  readonly order: number;
  readonly callback: () => void;
  cancelled: boolean;
}

export class FakeClock {
  #now = 0;
  #order = 0;
  readonly #entries: Entry[] = [];

  get now(): number {
    return this.#now;
  }

  /** Timers scheduled and not yet fired or cancelled. */
  get pending(): number {
    return this.#entries.filter(entry => !entry.cancelled).length;
  }

  readonly scheduleTimeout: ScheduleTimeout = (callback, delayMs = 0) => {
    const entry: Entry = {
      dueAt: this.#now + delayMs,
      order: this.#order,
      callback,
      cancelled: false,
    };
    this.#order += 1;
    this.#entries.push(entry);
    return () => {
      entry.cancelled = true;
    };
  };

  /** Move time forward by `ms`, firing each timer that comes due, earliest first. */
  advance(ms: number): void {
    const until = this.#now + ms;
    for (;;) {
      const due = this.#entries
        .filter(entry => !entry.cancelled && entry.dueAt <= until)
        .sort((a, b) => a.dueAt - b.dueAt || a.order - b.order)[0];
      if (due === undefined) break;
      this.#entries.splice(this.#entries.indexOf(due), 1);
      this.#now = Math.max(this.#now, due.dueAt);
      due.callback();
    }
    this.#now = until;
  }
}
