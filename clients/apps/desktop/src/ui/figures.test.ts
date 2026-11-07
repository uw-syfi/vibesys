import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {resultWords} from './orchestrator/fleet-words.js';
import {formatFigure} from './figures.js';

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
});
