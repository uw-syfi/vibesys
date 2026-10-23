/**
 * A small seeded generator for model-based tests (`fast-check` is not a dependency): the same seed
 * always yields the same sequence, so a failing seed is pinned as an example and replayed exactly.
 */
export class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0 || 1;
  }

  /** A float in [0, 1) (mulberry32). */
  next(): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let t = this.#state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  /** An integer in [0, n). */
  int(n: number): number {
    return Math.floor(this.next() * n);
  }

  bool(): boolean {
    return this.next() < 0.5;
  }

  /** One of `items`; `items` must not be empty. */
  pick<T>(items: readonly T[]): T {
    if (items.length === 0) throw new Error('Seeded.pick: no items');
    return items[this.int(items.length)] as T;
  }
}

/** The seeds every model-based test folds through: enough to reach rare interleavings. */
export const SEEDS: readonly number[] = Array.from({length: 300}, (_, index) => index + 1);

/** CI's existing nightly job selects deeper schedules; explicit failure seeds always run at PR depth. */
export function seedsAtDepth(pinned: readonly number[]): readonly number[] {
  const depth =
    (globalThis as {process?: {env: Record<string, string | undefined>}}).process?.env[
      'VIBESYS_TEST_DEPTH'
    ] ?? 'pr';
  if (depth !== 'pr' && depth !== 'nightly')
    throw new Error(`unknown VIBESYS_TEST_DEPTH: ${depth}`);
  return depth === 'nightly' ? [...new Set([...pinned, ...SEEDS])] : pinned;
}
