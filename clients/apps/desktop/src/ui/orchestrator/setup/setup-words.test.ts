import {describe, expect, it} from '@effect/vitest';
import {spendWords} from './setup-words.js';

describe('setup spend', () => {
  it('writes cents while it is small, and groups a large figure in thousands with none', () => {
    expect(spendWords(4.2)).toBe('$4.20');
    expect(spendWords(0)).toBe('$0.00');
    expect(spendWords(1234)).toBe('$1,234');
  });
});
