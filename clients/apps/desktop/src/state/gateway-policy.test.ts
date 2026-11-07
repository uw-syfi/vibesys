import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {afterFailure, MAX_DIALS} from './gateway-policy.js';

describe('gateway recovery policy', () => {
  it('waits 0.5, 1, 2 and 4 seconds between five dials, then goes offline', () => {
    const waits = [1, 2, 3, 4].map(failures => afterFailure(failures));
    expect(waits).toEqual([500, 1000, 2000, 4000].map(afterMs => ({_tag: 'redial', afterMs})));
    expect(afterFailure(MAX_DIALS)).toEqual({_tag: 'offline'});
  });

  it('never waits longer than four seconds, whatever the count', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 1000}), failures => {
        const verdict = afterFailure(failures);
        if (verdict._tag === 'redial') expect(verdict.afterMs).toBeLessThanOrEqual(4000);
      }),
    );
  });

  it('goes offline at once on a status a redial cannot change', () => {
    for (const code of [9, 13, 15])
      fc.assert(
        fc.property(fc.integer({min: 1, max: 4}), failures => {
          expect(afterFailure(failures, code)).toEqual({_tag: 'offline'});
        }),
      );
    expect(afterFailure(1, 14)._tag).toBe('redial');
  });
});
