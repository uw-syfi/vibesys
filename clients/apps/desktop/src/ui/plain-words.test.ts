import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {plural} from './plain-words.js';

describe('plural', () => {
  it('says the singular for one and the plural for every other count', () => {
    fc.assert(
      fc.property(fc.integer({min: 0, max: 1_000_000}), count => {
        expect(plural(count, 'machine', 'machines')).toBe(count === 1 ? 'machine' : 'machines');
      }),
      {seed: 7},
    );
  });
});
