import {describe, expect, test} from 'bun:test';
import {USAGE_WINDOW_MAX_HOURS, usageWindowRefusal} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {spanOf, USAGE_WINDOWS} from './window.js';

describe('spanOf', () => {
  test('property: every window the page offers is a window the helper answers', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const now = 1_000_000_000_000 + random.int(1_000_000_000);
      // The midnight may be at `now` (just after midnight) or up to a day before it.
      const midnight = now - random.int(24 * 3_600_000);
      for (const {value} of USAGE_WINDOWS) {
        const {since, until} = spanOf(value, {now, midnight});
        expect(until).toBe(now);
        expect(usageWindowRefusal([], since, until)).toBeNull();
      }
    }
  });

  test('today begins at the local midnight; a week and two weeks roll back from now', () => {
    const clock = {now: 1_000_000_000_000, midnight: 999_990_000_000};
    expect(spanOf('today', clock).since).toBe(clock.midnight);
    expect(clock.now - spanOf('week', clock).since).toBe(7 * 24 * 3_600_000);
    expect(clock.now - spanOf('fortnight', clock).since).toBe(USAGE_WINDOW_MAX_HOURS * 3_600_000);
  });
});
