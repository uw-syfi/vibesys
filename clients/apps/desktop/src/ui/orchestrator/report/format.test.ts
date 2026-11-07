import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {timeTicks} from '../../../state/index.js';
import {
  clip,
  formatAge,
  formatDollars,
  formatElapsed,
  formatFigure,
  seattleClock,
  seattleMoment,
  seattleOffsetMs,
} from './format.js';

const HOUR = 3_600_000;

describe('Seattle time', () => {
  it('is eight hours behind UTC in winter and seven in summer, whatever the instant', () => {
    fc.assert(
      fc.property(fc.integer({min: 1_700_000_000_000, max: 2_000_000_000_000}), at => {
        expect([-8 * HOUR, -7 * HOUR]).toContain(seattleOffsetMs(at));
      }),
      {seed: 31},
    );
  });

  it('moves its offset when the clocks change, on the night they change', () => {
    // 2026: summer time ends on Nov 1 at 2 AM local, which is 09:00 UTC.
    const change = Date.UTC(2026, 10, 1, 9);
    expect(seattleOffsetMs(change - 1000)).toBe(-7 * HOUR);
    expect(seattleOffsetMs(change)).toBe(-8 * HOUR);
  });

  it('writes a moment as the day and the time of day in Seattle', () => {
    // Monday, Nov 2 2026, 10:40 UTC is 2:40 AM in Seattle.
    const at = Date.UTC(2026, 10, 2, 10, 40);
    expect(seattleMoment(at)).toBe('Mon 2:40\u00a0AM');
    expect(seattleClock(at)).toBe('2:40\u00a0AM');
  });

  it('puts a time axis tick for a day at midnight in Seattle', () => {
    const from = Date.UTC(2026, 9, 28, 20);
    const ticks = timeTicks(from, from + 9 * 24 * HOUR, 5, seattleOffsetMs);
    expect(ticks.length).toBeGreaterThan(1);
    for (const tick of ticks) expect(seattleClock(tick)).toBe('12:00\u00a0AM');
  });
});

describe('ages and spans', () => {
  it('name an age as the span in the Runs list says it, and never in the future', () => {
    fc.assert(
      fc.property(fc.integer({min: -10_000, max: 1e10}), age => {
        const text = formatAge(age);
        expect(text).toMatch(/^(\d+d \d+h|\d+h \d+m|\d+m|\d+s) ago$/);
        // One formatter: an age is its span with `ago`, wherever a screen writes either.
        expect(text).toBe(`${formatElapsed(age)} ago`);
        if (age <= 0) expect(text).toBe('0s ago');
      }),
      {seed: 32},
    );
    expect(formatAge(23 * 60_000 + 5000)).toBe('23m ago');
    expect(formatAge(3 * HOUR + 59 * 60_000)).toBe('3h 59m ago');
    expect(formatAge(((5 * 24 + 19) * 60 * 60 + 30) * 1000)).toBe('5d 19h ago');
  });

  it('write an elapsed time in its two largest units, and grow with the time', () => {
    expect(formatElapsed(((2 * 24 + 6) * 60 * 60 + 30) * 1000)).toBe('2d 6h');
    expect(formatElapsed(42 * 60_000 + 10_000)).toBe('42m');
    expect(formatElapsed(-5)).toBe('0s');
    fc.assert(
      fc.property(fc.integer({min: 0, max: 1e9}), fc.integer({min: 0, max: 1e9}), (a, b) => {
        const [low, high] = a <= b ? [a, b] : [b, a];
        const seconds = (text: string): number => {
          const parts = [...text.matchAll(/(\d+)([dhms])/g)].map(
            m => [Number(m[1]), m[2]] as const,
          );
          const unit = {d: 86_400, h: 3600, m: 60, s: 1} as const;
          return parts.reduce((sum, [n, u]) => sum + n * unit[u as keyof typeof unit], 0);
        };
        expect(seconds(formatElapsed(low))).toBeLessThanOrEqual(seconds(formatElapsed(high)));
      }),
      {seed: 33},
    );
  });
});

describe('figures', () => {
  it('are three significant digits with a suffix for thousands', () => {
    expect(formatFigure(11_076)).toBe('11.1k');
    expect(formatFigure(15_600)).toBe('15.6k');
    expect(formatFigure(118)).toBe('118');
    expect(formatFigure(0.5123)).toBe('0.512');
    expect(formatFigure(2_310_000)).toBe('2.31M');
    expect(formatFigure(-0)).toBe('0');
  });

  it('are close to the number they stand for and keep its sign', () => {
    fc.assert(
      fc.property(fc.double({min: -1e12, max: 1e12, noNaN: true}), value => {
        const text = formatFigure(value);
        const scale = {k: 1e3, M: 1e6, G: 1e9}[text.at(-1) as 'k'] ?? 1;
        const back = Number.parseFloat(text) * scale;
        expect(Math.abs(back - value)).toBeLessThanOrEqual(Math.abs(value) * 0.0051 + 1e-9);
        if (back !== 0) expect(Math.sign(back)).toBe(Math.sign(value));
      }),
      {seed: 34},
    );
  });

  it('write dollars to the cent under ten, the tenth under a hundred, and whole above', () => {
    expect(formatDollars(4.2)).toBe('$4.20');
    expect(formatDollars(12.4)).toBe('$12.4');
    expect(formatDollars(318.4)).toBe('$318');
    expect(formatDollars(12_400)).toBe('$12,400');
  });

  it('cut a long text with an ellipsis at the limit and leave a short one whole', () => {
    fc.assert(
      fc.property(fc.string({maxLength: 80}), fc.integer({min: 1, max: 60}), (text, limit) => {
        const out = clip(text, limit);
        expect([...out].length).toBeLessThanOrEqual(Math.max(limit, 1));
        if (text.length <= limit) expect(out).toBe(text);
        else expect(out.endsWith('…')).toBe(true);
      }),
      {seed: 35},
    );
  });
});
