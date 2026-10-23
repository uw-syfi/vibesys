/** Held-reply scheduling shared by host-generation sequences and cross-process seam simulations. */
import {Seeded} from '@vibesys/store/testing';

/**
 * A request's reply, held until the test releases it. As a real call does, it rejects with the
 * signal's reason when the caller cancels, and an answer that comes after that is dropped.
 */
export class HeldReply<T> {
  readonly reply: Promise<T>;
  answered = false;
  readonly #resolve: (value: T) => void;
  readonly #reject: (error: unknown) => void;

  constructor(readonly signal: AbortSignal) {
    let resolve = (_value: T): void => {};
    let reject = (_error: unknown): void => {};
    this.reply = new Promise<T>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    this.#resolve = resolve;
    this.#reject = reject;
    const cancel = (): void => reject(signal.reason ?? new Error('cancelled'));
    if (signal.aborted) cancel();
    else signal.addEventListener('abort', cancel, {once: true});
    // The caller owns the rejection; a reply nobody awaits must not fail the process.
    this.reply.catch(() => {});
  }

  answer(value: T): void {
    this.answered = true;
    this.#resolve(value);
  }
  fail(error: unknown): void {
    this.answered = true;
    this.#reject(error);
  }
}

interface Delivery {
  readonly lane: object;
  readonly run: () => void;
}

/** The virtual timers a world owns; `FakeClock` satisfies it. */
export interface VirtualTimers {
  /** Fire the earliest due timer, moving virtual time to it; false when none is left. */
  runNext(): boolean;
}

/**
 * FIFO in each lane, seeded ordering between lanes, and virtual timers fired when no delivery is
 * left. Every wait is bounded in scheduler events (a delivery or a timer), and a world with
 * nothing left to deliver or fire fails with its seed instead of waiting for a wall-clock timeout.
 */
export class Schedule {
  readonly random: Seeded;
  readonly #queue: Delivery[] = [];
  events = 0;

  constructor(
    readonly seed: number,
    private readonly timers: VirtualTimers = {runNext: () => false},
  ) {
    this.random = new Seeded(seed);
  }

  post(lane: object, run: () => void): void {
    this.#queue.push({lane, run});
  }

  /** Explore until `done` holds. A stalled world or an exhausted bound is a failure. */
  async until(done: () => boolean, bound = 512): Promise<void> {
    const start = this.events;
    for (;;) {
      await settleMicrotasks();
      if (done()) return;
      if (this.events - start >= bound)
        throw new Error(`seed ${this.seed}: exceeded ${bound} scheduler events`);
      if (!this.#step())
        throw new Error(
          `seed ${this.seed}: stalled after ${this.events - start} scheduler events with nothing to deliver or fire`,
        );
    }
  }

  /**
   * Explore until nothing is left to deliver or fire, retries and their timers included. False
   * means the world was still busy after `bound` events, as a retry loop is.
   */
  async quiesce(bound = 512): Promise<boolean> {
    const start = this.events;
    for (;;) {
      await settleMicrotasks();
      if (this.events - start >= bound) return false;
      if (!this.#step()) return true;
    }
  }

  /** Explore until `operation` settles, and return its outcome. */
  async run<T>(operation: Promise<T>, bound = 512): Promise<T> {
    let settled = false;
    const result = operation.finally(() => {
      settled = true;
    });
    // Own the rejection immediately, while deliveries are still in flight.
    void result.catch(() => {});
    await this.until(() => settled, bound);
    return result;
  }

  #step(): boolean {
    if (this.#queue.length > 0) {
      const lanes = [...new Set(this.#queue.map(delivery => delivery.lane))];
      const lane = this.random.pick(lanes);
      const index = this.#queue.findIndex(delivery => delivery.lane === lane);
      const [delivery] = this.#queue.splice(index, 1);
      this.events += 1;
      delivery?.run();
      return true;
    }
    if (!this.timers.runNext()) return false;
    this.events += 1;
    return true;
  }
}

/**
 * Let every promise continuation run before the next event is chosen: one event-loop turn flushes
 * the microtask queue, however long the continuation chain. It reads no clock.
 */
function settleMicrotasks(): Promise<void> {
  return new Promise(resolve => setImmediate(resolve));
}
