/** A queue a producer pushes into and a consumer iterates, ending when closed or failed. */
export class AsyncQueue<T> implements AsyncIterable<T> {
  readonly #items: T[] = [];
  #waiting: ((step: IteratorResult<T>) => void) | null = null;
  #failure: {readonly error: unknown} | null = null;
  #rejectWaiting: ((error: unknown) => void) | null = null;
  #closed = false;

  push(item: T): void {
    if (this.#closed) return;
    const waiting = this.#waiting;
    if (waiting === null) {
      this.#items.push(item);
      return;
    }
    this.#waiting = null;
    this.#rejectWaiting = null;
    waiting({value: item, done: false});
  }

  close(): void {
    if (this.#closed) return;
    this.#closed = true;
    const waiting = this.#waiting;
    this.#waiting = null;
    this.#rejectWaiting = null;
    waiting?.({value: undefined, done: true});
  }

  fail(error: unknown): void {
    if (this.#closed) return;
    this.#closed = true;
    const reject = this.#rejectWaiting;
    this.#waiting = null;
    this.#rejectWaiting = null;
    if (reject === null) this.#failure = {error};
    else reject(error);
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return {
      next: () => {
        const item = this.#items.shift();
        if (item !== undefined) return Promise.resolve({value: item, done: false});
        if (this.#failure !== null) {
          const {error} = this.#failure;
          this.#failure = null;
          return Promise.reject(error);
        }
        if (this.#closed) return Promise.resolve({value: undefined, done: true});
        return new Promise((resolve, reject) => {
          this.#waiting = resolve;
          this.#rejectWaiting = reject;
        });
      },
    };
  }
}
