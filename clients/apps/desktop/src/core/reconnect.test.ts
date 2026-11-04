import {describe, expect, it} from '@effect/vitest';
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import fc from 'fast-check';
import {ceilingMs, defaultReconnect, delayMs, failuresAfter, reconnectDelay} from './reconnect.js';

const policy = defaultReconnect;
const failures = fc.integer({min: 1, max: 200});
const unit = fc.double({min: 0, max: 1, noNaN: true, maxExcluded: true});

describe('the reconnect wait', () => {
  it('is between half the ceiling and the ceiling, and never above the cap', () => {
    fc.assert(
      fc.property(failures, unit, (n, u) => {
        const wait = delayMs(policy, n, u);
        const ceiling = ceilingMs(policy, n);
        expect(wait).toBeGreaterThanOrEqual(Math.floor(ceiling / 2));
        expect(wait).toBeLessThanOrEqual(ceiling);
        expect(wait).toBeLessThanOrEqual(policy.capMs);
      }),
    );
  });

  it('has a ceiling that doubles from the base until the cap and then stays there', () => {
    fc.assert(
      fc.property(failures, n => {
        expect(ceilingMs(policy, n)).toBeLessThanOrEqual(ceilingMs(policy, n + 1));
        expect(ceilingMs(policy, n + 1)).toBe(Math.min(policy.capMs, 2 * ceilingMs(policy, n)));
      }),
    );
    expect(ceilingMs(policy, 1)).toBe(policy.baseMs);
    expect(ceilingMs(policy, 100)).toBe(policy.capMs);
  });

  it('starts over only after a Ready period of the stable length, never for a link that flaps faster', () => {
    fc.assert(
      fc.property(failures, fc.integer({min: 0, max: policy.stableAfterMs - 1}), (n, up) => {
        expect(failuresAfter(policy, n, up)).toBe(n + 1);
        expect(failuresAfter(policy, n, null)).toBe(n + 1);
      }),
    );
    fc.assert(
      fc.property(failures, fc.integer({min: policy.stableAfterMs, max: 10 ** 9}), (n, up) => {
        expect(failuresAfter(policy, n, up)).toBe(1);
      }),
    );
  });

  it.effect('draws its jitter from the seeded Random, so the same seed waits the same', () =>
    Effect.gen(function* () {
      const waits = yield* Effect.all([1, 2, 3, 4].map(n => reconnectDelay(policy, n))).pipe(
        Random.withSeed('reconnect'),
      );
      const again = yield* Effect.all([1, 2, 3, 4].map(n => reconnectDelay(policy, n))).pipe(
        Random.withSeed('reconnect'),
      );
      expect(waits.map(Duration.toMillis)).toEqual(again.map(Duration.toMillis));
    }),
  );
});
