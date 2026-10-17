import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {roleSeries, SERIES_SLOTS} from './role-series.js';

/** A role's color is one palette slot, and the same slot every time anything asks. */

const KNOWN = ['orchestrator', 'implementer', 'reviewer', 'judge', 'profiler', 'mutator'];

function randomRole(random: Seeded): string {
  const letters = 'abcdefghijklmnopqrstuvwxyz-_';
  return Array.from({length: 1 + random.int(14)}, () =>
    letters.charAt(random.int(letters.length)),
  ).join('');
}

describe('a role series', () => {
  test('is a palette slot, the same on every ask, for any role name', () => {
    for (const seed of SEEDS) {
      const role = randomRole(new Seeded(seed));
      const slot = roleSeries(role);
      expect(Number.isInteger(slot)).toBe(true);
      expect(slot).toBeGreaterThanOrEqual(0);
      expect(slot).toBeLessThan(SERIES_SLOTS);
      expect(roleSeries(role)).toBe(slot);
    }
  });

  test('is different for each role the searches use, so their bars and legend entries never match', () => {
    const slots = KNOWN.map(roleSeries);
    expect(new Set(slots).size).toBe(KNOWN.length);
  });
});
