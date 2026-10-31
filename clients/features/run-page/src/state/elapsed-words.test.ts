import {expect, test} from 'bun:test';
import * as fc from 'fast-check';
import {elapsedWords} from './span-reading.js';

test('writes time into a turn to the second below an hour, then to the minute', () => {
  const words: [number, string][] = [
    [0, '0s'],
    [0.9, '0s'],
    [59, '59s'],
    [60, '1m 00s'],
    [64, '1m 04s'],
    [725, '12m 05s'],
    [3599, '59m 59s'],
    [3600, '1h 00m'],
    [7500, '2h 05m'],
    [-3, '0s'],
  ];
  for (const [seconds, expected] of words) expect(elapsedWords(seconds)).toBe(expected);
});

test('reads back as the time it was given, and never runs backwards as time grows', () => {
  const read = (text: string): number =>
    [...text.matchAll(/(\d+)([hms])/g)].reduce(
      (total, [, count, unit]) =>
        total + Number(count) * ({h: 3600, m: 60, s: 1}[unit as 'h'] ?? 0),
      0,
    );
  fc.assert(
    fc.property(fc.integer({min: 0, max: 400_000}), fc.integer({min: 0, max: 400_000}), (a, b) => {
      const [early, late] = a <= b ? [a, b] : [b, a];
      const exact = early < 3600 ? early : early - (early % 60);
      expect(read(elapsedWords(early))).toBe(exact);
      expect(read(elapsedWords(early))).toBeLessThanOrEqual(read(elapsedWords(late)));
    }),
  );
});
