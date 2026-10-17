import {describe, expect, test} from 'bun:test';
import {NO_TOKENS, type TokenTally} from '@vibesys/run-state';
import {
  compactTokens,
  tokensColumn,
  tokensFigure,
  tokensLabel,
  tokensReading,
  tokensTitle,
} from './token-words.js';

const TALLY: TokenTally = {
  input: 1_234_567,
  cacheRead: 1_000_000,
  cacheWrite: null,
  output: 12_345,
  reasoning: 2_000,
};

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

describe('a tally in words', () => {
  test('the figure is input plus output, and the label says tokens', () => {
    expect(tokensFigure(TALLY)).toBe('1.2M');
    expect(tokensLabel(TALLY)).toBe('1.2M tokens');
  });

  test('the title lists the counts the provider reported, in full, and none it did not', () => {
    expect(tokensTitle(TALLY)).toBe(
      'Input 1,234,567 (1,000,000 cached)\nOutput 12,345 (2,000 reasoning)\nTotal 1,246,912',
    );
    expect(tokensTitle({...NO_TOKENS, output: 40})).toBe('Output 40\nTotal 40');
    expect(tokensTitle({...TALLY, cacheWrite: 500})).toContain(
      '(1,000,000 cached, 500 written to cache)',
    );
    expect(tokensTitle({...TALLY, cacheWrite: 0})).not.toContain('written');
  });

  test('a tally with neither input nor output has no figure, label or reading', () => {
    expect(tokensFigure(NO_TOKENS)).toBeNull();
    expect(tokensLabel(NO_TOKENS)).toBeNull();
    expect(tokensReading(NO_TOKENS)).toBeNull();
    expect(tokensReading(null)).toBeNull();
    expect(tokensReading(undefined)).toBeNull();
  });

  test('a real zero reads as zero, not as missing', () => {
    expect(tokensLabel({...NO_TOKENS, input: 0, output: 0})).toBe('0 tokens');
  });

  test('the reading pairs the figure with the full counts', () => {
    expect(tokensReading(TALLY)).toEqual({label: '1.2M', title: tokensTitle(TALLY)});
  });
});

describe('a tokens column', () => {
  test('gives a row without a reading a blank cell once any row has one, so figures line up', () => {
    const cells = tokensColumn([TALLY, NO_TOKENS, null, undefined]);
    expect(cells.map(cell => cell?.label)).toEqual(['1.2M', '', '', '']);
  });

  test('has no cells at all when no row has a reading', () => {
    expect(tokensColumn([NO_TOKENS, null, undefined])).toEqual([null, null, null]);
    expect(tokensColumn([])).toEqual([]);
  });
});
