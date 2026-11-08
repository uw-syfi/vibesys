import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {formatFigure} from './figures.js';
import {resultWords} from './orchestrator/fleet-words.js';

describe('a measured value', () => {
  it('reads the same on the Runs list as in the report, for any value', () => {
    fc.assert(
      fc.property(fc.double({min: -1e12, max: 1e12, noNaN: true}), value => {
        const row = resultWords({name: 'score', value, unit: 'tok/s', percentOfBound: null});
        expect(row).toBe(`${formatFigure(value)} tok/s`);
      }),
      {seed: 51},
    );
  });

  it('keeps the digits a summary writes, so a tile never shows 17 beside 17.01', () => {
    expect(formatFigure(17.01)).toBe('17.01');
    expect(formatFigure(17)).toBe('17');
    expect(formatFigure(118.4)).toBe('118.4');
  });

  it('rounds into the next unit before picking a suffix, so it never writes 1000 of one', () => {
    expect(formatFigure(999_999)).toBe('1M');
    expect(formatFigure(999.96)).toBe('1k');
    expect(formatFigure(999_950)).toBe('1M');
    expect(formatFigure(-999_999)).toBe('-1M');
  });

  it('never writes 1000 or more of a unit that has a bigger one above it', () => {
    // Values this close to zero are outside the rollover this test pins: `toPrecision` itself
    // writes them in exponential notation, same as before this fix, for any value or none.
    const notTooSmall = (value: number) => value === 0 || Math.abs(value) >= 1e-5;
    // A uniform double over the whole domain almost never lands in the narrow band right below a
    // seam (999,950 to 1,000,000; 999.95M to 1,000M) where the rollover bug lived, so half the
    // runs are drawn from those bands directly.
    const nearASeam = fc
      .tuple(fc.constantFrom(1e6, 1e9), fc.double({min: 0, max: 50, noNaN: true}))
      .map(([seam, below]) => seam - below);
    fc.assert(
      fc.property(
        fc
          .oneof(
            fc.double({min: -999.9499e9, max: 999.9499e9, noNaN: true}),
            nearASeam,
            nearASeam.map(value => -value),
          )
          .filter(notTooSmall),
        value => {
          const text = formatFigure(value);
          const match = /^-?(\d+(?:\.\d+)?)([kMG]?)$/.exec(text);
          if (match === null) throw new Error(`"${text}" does not look like a figure`);
          expect(Math.abs(Number(match[1]))).toBeLessThan(1000);
        },
      ),
      {seed: 51},
    );
  });
});
