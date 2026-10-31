import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {axisDollars, dollars} from './format.js';

describe('dollars', () => {
  test('property: from a cent up, thousands are grouped by three and the value survives to the cent', () => {
    for (const seed of SEEDS.slice(0, 200)) {
      const rng = new Seeded(seed);
      const amount = rng.int(1_000_000_000) / 100 + 0.01;
      const written = dollars(amount);
      expect(written, `seed ${seed}`).toMatch(/^\$\d{1,3}(,\d{3})*\.\d{2}$/);
      expect(Math.abs(Number(written.replace(/[$,]/g, '')) - amount), `seed ${seed}`).toBeLessThan(
        0.006,
      );
    }
  });

  test('property: below a cent a price keeps two significant digits and is never written as zero', () => {
    for (const seed of SEEDS.slice(0, 200)) {
      const amount = (new Seeded(seed).int(99) + 1) / 10 ** (3 + new Seeded(seed + 1).int(4));
      expect(Number(dollars(amount).slice(1)), `seed ${seed}`).toBeGreaterThan(0);
    }
  });

  test('nothing, a loss and what is not a number are zero dollars', () => {
    for (const amount of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(dollars(amount)).toBe('$0.00');
    }
  });
});

describe('axisDollars', () => {
  test('a whole number of dollars drops its cents, and anything else is as dollars writes it', () => {
    expect(axisDollars(300)).toBe('$300');
    expect(axisDollars(12_000)).toBe('$12,000');
    expect(axisDollars(0)).toBe('$0');
    expect(axisDollars(0.5)).toBe(dollars(0.5));
  });
});
