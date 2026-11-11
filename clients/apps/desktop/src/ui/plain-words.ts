/** Small words every screen writes the same way, so two pages never keep two copies of them. */

/** What a reading or a count nobody reported is called where a word fits better than a dash. */
export const UNKNOWN = 'unknown';

/** `one` for exactly one, `many` for any other count (including none). */
export const plural = (count: number, one: string, many: string): string =>
  count === 1 ? one : many;
