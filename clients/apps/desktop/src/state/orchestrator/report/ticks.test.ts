import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {valueTicks} from './ticks.js';

describe('value ticks', () => {
  it('are increasing, inside the range, few, and round multiples of one step', () => {
    fc.assert(
      fc.property(
        fc.double({min: -1e6, max: 1e6, noNaN: true}),
        fc.double({min: 1e-6, max: 1e6, noNaN: true}),
        fc.integer({min: 1, max: 8}),
        (low, span, wanted) => {
          const high = low + span;
          const ticks = valueTicks(low, high, wanted);
          expect(ticks.length).toBeLessThanOrEqual(2 * wanted + 1);
          ticks.forEach((tick, i) => {
            expect(tick).toBeGreaterThanOrEqual(low - 1e-9 * Math.abs(low));
            expect(tick).toBeLessThanOrEqual(high + 1e-9 * Math.abs(high));
            if (i > 0) expect(tick).toBeGreaterThan(ticks[i - 1] ?? Number.NEGATIVE_INFINITY);
          });
          const [a, b, c] = ticks;
          if (a !== undefined && b !== undefined && c !== undefined) {
            expect(Math.abs(c - b - (b - a))).toBeLessThanOrEqual(1e-9 * Math.max(1, Math.abs(c)));
          }
        },
      ),
      // Pinned: when `step` is many orders of magnitude smaller than `low`, rounding two distinct
      // multiples of `step` to 12 significant figures can land on the same value, which the default
      // run count rarely draws (found only by stress-running this property past 100 draws).
      {seed: 21, examples: [[99999.99999920001, 0.000001, 5]]},
    );
  });

  it('are none for an empty, reversed or non-finite range', () => {
    expect(valueTicks(1, 1, 4)).toEqual([]);
    expect(valueTicks(2, 1, 4)).toEqual([]);
    expect(valueTicks(0, Number.POSITIVE_INFINITY, 4)).toEqual([]);
    expect(valueTicks(0, 1, 0)).toEqual([]);
  });
});
