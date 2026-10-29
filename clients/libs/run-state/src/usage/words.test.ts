import {describe, expect, test} from 'bun:test';
import {compactTokens} from './words.js';

describe('compactTokens', () => {
  test('writes a count below a thousand whole, then k and M with one decimal', () => {
    expect(compactTokens(0)).toBe('0');
    expect(compactTokens(999)).toBe('999');
    expect(compactTokens(1_000)).toBe('1k');
    expect(compactTokens(1_250)).toBe('1.3k');
    expect(compactTokens(48_200)).toBe('48.2k');
    expect(compactTokens(1_200_000)).toBe('1.2M');
    expect(compactTokens(2_500_000_000)).toBe('2.5B');
  });

  test('rolls over to the next unit at the rounding edge, never writing 1000k', () => {
    expect(compactTokens(999_949)).toBe('999.9k');
    expect(compactTokens(999_950)).toBe('1M');
    expect(compactTokens(999_949_999)).toBe('999.9M');
    expect(compactTokens(999_950_000)).toBe('1B');
  });

  test('stays within a tenth of a unit of the count, for any count', () => {
    let state = 7;
    for (let index = 0; index < 500; index += 1) {
      state = (Math.imul(state, 1_103_515_245) + 12_345) >>> 0;
      const count = Math.floor((state / 2 ** 32) ** 3 * 5_000_000_000);
      const text = compactTokens(count);
      expect(text).toMatch(/^\d{1,3}(\.\d)?[kMB]?$/);
      const scale = text.endsWith('k')
        ? 1e3
        : text.endsWith('M')
          ? 1e6
          : text.endsWith('B')
            ? 1e9
            : 1;
      expect(Math.abs(Number.parseFloat(text) * scale - count)).toBeLessThanOrEqual(
        scale * 0.05 + 1e-6,
      );
    }
  });
});
