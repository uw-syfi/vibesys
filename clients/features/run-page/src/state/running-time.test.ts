/**
 * The run's running time over generated sessions: the run clock with the stretches the run stood
 * stopped taken out, the same mapping for every chart and for the time bar.
 */
import {describe, expect, test} from 'bun:test';
import type {RunSession} from '@vibesys/run-state/skeleton';
import fc from 'fast-check';
import {marksAlong, placesAlong, runningTimeOf} from './running-time.js';

/** Sessions that follow one another with stopped gaps between them (a gap may be none). */
const sessions: fc.Arbitrary<readonly RunSession[]> = fc
  .array(
    fc.record({work: fc.integer({min: 1, max: 5000}), stopped: fc.integer({min: 0, max: 90000})}),
    {
      minLength: 1,
      maxLength: 6,
    },
  )
  .map(parts => {
    let at = 0;
    return parts.map(part => {
      const from = at + part.stopped;
      at = from + part.work;
      return {from, to: at};
    });
  });

describe('running time', () => {
  test('is the time the sessions worked: the gaps between them are not on it', () => {
    fc.assert(
      fc.property(sessions, each => {
        const time = runningTimeOf(each, 0);
        const worked = each.reduce((sum, session) => sum + (session.to - session.from), 0);
        const first = each[0]?.from ?? 0;
        // The clock stood at `first` before the first session: the axis starts at the run's start.
        expect(time.at(each.at(-1)?.to ?? 0)).toBe(first + worked);
      }),
    );
  });

  test('never goes back, and never runs faster than the clock', () => {
    fc.assert(
      fc.property(
        sessions,
        fc.array(fc.integer({min: 0, max: 800000}), {maxLength: 12}),
        (each, times) => {
          const time = runningTimeOf(each, 0);
          const ordered = [...times].sort((a, b) => a - b);
          for (let at = 1; at < ordered.length; at += 1) {
            const [before, after] = [ordered[at - 1] ?? 0, ordered[at] ?? 0];
            const moved = time.at(after) - time.at(before);
            expect(moved).toBeGreaterThanOrEqual(0);
            expect(moved).toBeLessThanOrEqual(after - before);
          }
        },
      ),
    );
  });

  test('is the clock itself inside one session', () => {
    fc.assert(
      fc.property(sessions, fc.double({min: 0, max: 1, noNaN: true}), (each, share) => {
        const time = runningTimeOf(each, 0);
        for (const session of each) {
          const [a, b] = [session.from, session.from + share * (session.to - session.from)];
          expect(time.at(b) - time.at(a)).toBeCloseTo(b - a, 6);
        }
      }),
    );
  });

  test('stands still across a stopped gap, and marks one resume for each session after the first', () => {
    fc.assert(
      fc.property(sessions, each => {
        const time = runningTimeOf(each, 0);
        expect(time.resumes).toHaveLength(each.length - 1);
        for (let at = 1; at < each.length; at += 1) {
          const [ended, began] = [each[at - 1]?.to ?? 0, each[at]?.from ?? 0];
          expect(time.at(began)).toBe(time.at(ended));
          expect(time.resumes[at - 1]).toBe(time.at(began));
        }
      }),
    );
  });

  test('is the clock as it stands when no session is known', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 0, max: 100000}),
        fc.integer({min: 0, max: 100000}),
        (start, at) => {
          const time = runningTimeOf([], start);
          expect(time.at(at)).toBe(Math.max(0, at - start));
          expect(time.resumes).toEqual([]);
        },
      ),
    );
  });
});

describe('places along the time bar', () => {
  test('are 0 to 1, never go back, and keep the proportions of the running time', () => {
    fc.assert(
      fc.property(
        fc.array(fc.integer({min: 0, max: 1000000}), {minLength: 2, maxLength: 30}),
        times => {
          const seconds = [...times].sort((a, b) => a - b);
          const places = placesAlong(seconds);
          expect(places).toHaveLength(seconds.length);
          for (const place of places) {
            expect(place).toBeGreaterThanOrEqual(0);
            expect(place).toBeLessThanOrEqual(1);
          }
          expect(places[0]).toBe(0);
          for (let at = 1; at < places.length; at += 1) {
            expect(places[at] ?? 0).toBeGreaterThanOrEqual(places[at - 1] ?? 0);
          }
          const [first, last] = [seconds[0] ?? 0, seconds.at(-1) ?? 0];
          if (last > first) {
            expect(places.at(-1)).toBe(1);
            const middle = Math.floor(seconds.length / 2);
            expect(places[middle]).toBeCloseTo(
              ((seconds[middle] ?? 0) - first) / (last - first),
              9,
            );
          }
        },
      ),
    );
  });

  test('spread evenly when the run never advanced', () => {
    expect(placesAlong([5, 5, 5])).toEqual([0, 0.5, 1]);
    expect(placesAlong([7])).toEqual([0]);
    expect(placesAlong([])).toEqual([]);
  });
});

describe('marks along the time bar', () => {
  const track = fc
    .array(fc.integer({min: 0, max: 1000000}), {minLength: 2, maxLength: 30})
    .map(times => [...times].sort((a, b) => a - b));

  test('stand where the position of their time stands, and nowhere else', () => {
    fc.assert(
      fc.property(track, fc.nat(29), (seconds, pick) => {
        const places = placesAlong(seconds);
        const at = pick % seconds.length;
        const marks = marksAlong(seconds, [seconds[at] ?? 0]);
        if ((seconds.at(-1) ?? 0) <= (seconds[0] ?? 0)) return expect(marks).toEqual([]);
        expect(marks).toHaveLength(1);
        expect(marks[0]).toBeCloseTo(places[at] ?? 0, 9);
      }),
    );
  });

  test('are ascending, repeat no place, and keep to the track', () => {
    fc.assert(
      fc.property(track, fc.array(fc.integer({min: -10000, max: 1100000})), (seconds, times) => {
        const marks = marksAlong(seconds, times);
        for (const [at, place] of marks.entries()) {
          expect(place).toBeGreaterThanOrEqual(0);
          expect(place).toBeLessThanOrEqual(1);
          if (at > 0) expect(place).toBeGreaterThan(marks[at - 1] ?? 0);
        }
      }),
    );
  });
});
