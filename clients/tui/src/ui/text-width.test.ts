import {describe, expect, it} from 'bun:test';
import {displayWidth, padToWidth, truncateToWidth} from './text-width.js';

describe('padToWidth', () => {
  it('pads by cells, not code units, so CJK text fills the same column', () => {
    expect(padToWidth('abc', 6)).toBe('abc   ');
    // Two ideographs are two code units but four cells: padEnd would add four
    // spaces here and make the cell eight cells wide.
    expect(padToWidth('漢字', 6)).toBe('漢字  ');
    expect(displayWidth(padToWidth('漢字', 6))).toBe(6);
  });

  it('returns text at or past the budget unchanged', () => {
    expect(padToWidth('abcdef', 6)).toBe('abcdef');
    expect(padToWidth('漢字漢字', 6)).toBe('漢字漢字');
    expect(padToWidth('x', 0)).toBe('x');
  });

  it('fills the cell a truncation left short of a wide character', () => {
    // '字' would straddle the four-cell budget, so truncateToWidth stops one
    // cell short; padding restores the exact column width.
    const cut = truncateToWidth('a漢字', 4);
    expect(cut).toBe('a漢');
    expect(padToWidth(cut, 4)).toBe('a漢 ');
    expect(displayWidth(padToWidth(cut, 4))).toBe(4);
  });
});
