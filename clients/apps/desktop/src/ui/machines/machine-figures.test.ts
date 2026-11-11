import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {memoryFigure, percentFigure} from './machine-figures.js';

const GIB = 1024 ** 3;

describe('the figures of a machine row', () => {
  it('write memory with one decimal at most and none when whole, in GiB or TiB by the total', () => {
    expect(memoryFigure({usedBytes: 41 * GIB, totalBytes: 128 * GIB})).toBe('41 / 128 GiB');
    expect(memoryFigure({usedBytes: 1840 * GIB, totalBytes: 2048 * GIB})).toBe('1.8 / 2 TiB');
    expect(memoryFigure({usedBytes: 4.5 * GIB, totalBytes: 16 * GIB})).toBe('4.5 / 16 GiB');
  });

  it('never write more than one decimal, whatever the bytes', () => {
    fc.assert(
      fc.property(
        fc.nat(1024 * GIB * 4),
        fc.integer({min: 1, max: 1024 * GIB * 4}),
        (used, total) => {
          expect(memoryFigure({usedBytes: used, totalBytes: total})).toMatch(
            /^\d+(\.\d)? \/ \d+(\.\d)? (GiB|TiB)$/,
          );
        },
      ),
    );
  });

  it('write a percent with one decimal at most', () => {
    expect(percentFigure(37.5)).toBe('37.5%');
    expect(percentFigure(46)).toBe('46%');
    fc.assert(
      fc.property(fc.double({min: 0, max: 100, noNaN: true}), value => {
        expect(percentFigure(value)).toMatch(/^\d+(\.\d)?%$/);
      }),
    );
  });
});
