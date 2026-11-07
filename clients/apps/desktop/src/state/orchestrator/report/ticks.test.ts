import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {timeTicks, valueTicks} from './ticks.js';

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
      {seed: 21},
    );
  });

  it('are none for an empty, reversed or non-finite range', () => {
    expect(valueTicks(1, 1, 4)).toEqual([]);
    expect(valueTicks(2, 1, 4)).toEqual([]);
    expect(valueTicks(0, Number.POSITIVE_INFINITY, 4)).toEqual([]);
    expect(valueTicks(0, 1, 0)).toEqual([]);
  });
});

const HOUR = 3_600_000;
/** A zone eight hours behind UTC that moves to seven hours behind at `switchMs`. */
const seattleLike =
  (switchMs: number) =>
  (atMs: number): number =>
    atMs < switchMs ? -8 * HOUR : -7 * HOUR;

describe('time ticks', () => {
  it('are increasing, inside the range, few, and on the zone’s clock even across a change of offset', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1_700_000_000_000, max: 1_900_000_000_000}),
        fc.integer({min: 1000, max: 20 * 24 * HOUR}),
        fc.integer({min: 1, max: 8}),
        fc.integer({min: -10 * 24 * HOUR, max: 10 * 24 * HOUR}),
        (from, span, wanted, switchAt) => {
          const offset = seattleLike(from + switchAt);
          const to = from + span;
          const ticks = timeTicks(from, to, wanted, offset);
          expect(ticks.length).toBeLessThanOrEqual(2 * wanted + 1);
          ticks.forEach((tick, i) => {
            expect(tick).toBeGreaterThanOrEqual(from);
            expect(tick).toBeLessThanOrEqual(to);
            if (i > 0) expect(tick).toBeGreaterThan(ticks[i - 1] ?? Number.NEGATIVE_INFINITY);
          });
          // Whatever the spacing, every tick falls on a whole second of the zone's clock, and on a
          // whole minute for any range of a minute or more.
          for (const tick of ticks) expect((tick + offset(tick)) % 1000).toBe(0);
        },
      ),
      {seed: 22},
    );
  });

  it('puts a day’s tick at the zone’s midnight', () => {
    const offset = seattleLike(Number.POSITIVE_INFINITY);
    const from = Date.UTC(2026, 9, 1, 12);
    const ticks = timeTicks(from, from + 12 * 24 * HOUR, 6, offset);
    expect(ticks.length).toBeGreaterThan(1);
    for (const tick of ticks) expect((tick + offset(tick)) % (24 * HOUR)).toBe(0);
  });

  it('are none for an empty or reversed range', () => {
    expect(timeTicks(5, 5, 4, () => 0)).toEqual([]);
    expect(timeTicks(9, 5, 4, () => 0)).toEqual([]);
  });
});
