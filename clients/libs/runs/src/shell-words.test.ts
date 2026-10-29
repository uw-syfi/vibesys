import {describe, expect, test} from 'bun:test';
import {canonicalShellWords, ShellWordsError, shellWords} from '@vibesys/runs';
import {Seeded} from '@vibesys/store/testing';

describe('shellWords', () => {
  test('quotes group words and blanks separate them', () => {
    expect(shellWords('--goal "make it fast"  --rounds 3')).toEqual([
      '--goal',
      'make it fast',
      '--rounds',
      '3',
    ]);
    expect(shellWords(`a'b c'd "e\\"f" g\\ h ''`)).toEqual(['ab cd', 'e"f', 'g h', '']);
    expect(shellWords('  ')).toEqual([]);
  });

  test('the canonical rendering round-trips every generated argument vector', () => {
    const random = new Seeded(99);
    const alphabet = [...`ab -'"$\`\\\t;*é😀`];
    for (let round = 0; round < 1_000; round += 1) {
      const words = Array.from({length: random.int(8)}, () =>
        Array.from({length: random.int(16)}, () => random.pick(alphabet)).join(''),
      );
      const rendered = canonicalShellWords(words);
      expect(shellWords(rendered)).toEqual(words);
      expect(canonicalShellWords(shellWords(rendered))).toBe(rendered);
    }
  });

  test('an unclosed quote or a trailing backslash is refused', () => {
    for (const line of ['"a', "'a", 'a\\']) expect(() => shellWords(line)).toThrow(ShellWordsError);
  });
});
