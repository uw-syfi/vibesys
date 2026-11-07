import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {afterFailure, CONNECTION_LINGER_MS, MAX_DIALS} from './gateway-policy.js';

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
          expect(afterFailure(failures, {code, refused: false})).toEqual({_tag: 'offline'});
        }),
      );
    expect(afterFailure(1, {code: 14, refused: false})._tag).toBe('redial');
  });

  it('goes offline at once on a refusal, whatever the count and status: no redial changes it', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 1000}),
        fc.option(fc.integer({min: 0, max: 16}), {nil: undefined}),
        (failures, code) => {
          expect(afterFailure(failures, {code, refused: true})).toEqual({_tag: 'offline'});
        },
      ),
    );
  });

  it('keeps the connection up for a short while after its last call, a policy value', () => {
    expect(CONNECTION_LINGER_MS).toBeGreaterThan(0);
    expect(CONNECTION_LINGER_MS).toBeLessThanOrEqual(30_000);
  });
});
