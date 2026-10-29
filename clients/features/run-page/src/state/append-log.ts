/**
 * An immutable, append-only list whose appends cost the items appended, not the list's length.
 *
 * Each `AppendLog` is a view of the first `length` items of a backing array it may share with the
 * logs appended from it. Appending to the newest view of a backing array pushes onto that array
 * and returns a longer view, so an older view never sees the new items (it keeps its own
 * `length`); appending to an older view (a branch) copies its items once. Following a run appends
 * to the newest view every time, so following it costs time linear in its events overall.
 */
export class AppendLog<T extends NonNullable<unknown>> implements Iterable<T> {
  readonly #items: T[];
  readonly length: number;

  private constructor(items: T[], length: number) {
    this.#items = items;
    this.length = length;
  }

  static of<T extends NonNullable<unknown>>(items: readonly T[]): AppendLog<T> {
    return new AppendLog([...items], items.length);
  }

  /** This log with `added` after its items; this log itself when `added` is empty. */
  append(added: readonly T[]): AppendLog<T> {
    if (added.length === 0) return this;
    const items =
      this.length === this.#items.length ? this.#items : this.#items.slice(0, this.length);
    items.push(...added);
    return new AppendLog(items, items.length);
  }

  /** The item at `index` (negative counts from the end), or undefined outside the log. */
  at(index: number): T | undefined {
    const at = index < 0 ? this.length + index : index;
    return at >= 0 && at < this.length ? this.#items[at] : undefined;
  }

  /** The items from `start` on, as a fresh array: costs the items returned. */
  slice(start: number, end: number = this.length): T[] {
    return this.#items.slice(Math.max(0, start), Math.min(end, this.length));
  }

  /** How many of the leading items satisfy `before` (which holds of a prefix), found by halving. */
  countWhile(before: (item: T) => boolean): number {
    let low = 0;
    let high = this.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      const item = this.#items[middle];
      if (item !== undefined && before(item)) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  /** The last of the leading items that satisfy `before`, or undefined when none does. */
  lastWhile(before: (item: T) => boolean): T | undefined {
    const count = this.countWhile(before);
    return count === 0 ? undefined : this.#items[count - 1];
  }

  *[Symbol.iterator](): Iterator<T> {
    for (let index = 0; index < this.length; index += 1) {
      const item = this.#items[index];
      if (item !== undefined) yield item;
    }
  }

  toArray(): T[] {
    return this.slice(0);
  }
}
