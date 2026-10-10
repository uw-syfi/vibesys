import {describe, expect, test} from 'bun:test';
import {ShellWordsError, shellWords} from './shell-words.js';

/** Quote `word` the way a user might, so `shellWords` must give it back. */
function quoted(word: string, style: number): string {
  if (style === 0) return `'${word.replaceAll("'", `'\\''`)}'`;
  if (style === 1) return `"${word.replace(/["\\$`]/g, char => `\\${char}`)}"`;
  return word.replace(/[\s'"\\]/g, char => `\\${char}`) || "''";
}

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

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

  test('any words, quoted any way, split back into themselves', () => {
    const random = generator(99);
    const alphabet = [...`ab -'"$\`\\ \t;*é`];
    for (let round = 0; round < 500; round += 1) {
      const words = Array.from({length: Math.floor(random() * 5)}, () =>
        Array.from(
          {length: Math.floor(random() * 6)},
          () => alphabet[Math.floor(random() * alphabet.length)],
        ).join(''),
      );
      const line = words.map(word => quoted(word, Math.floor(random() * 3))).join(' ');
      expect(shellWords(line)).toEqual(words);
    }
  });

  test('an unclosed quote or a trailing backslash is refused', () => {
    for (const line of ['"a', "'a", 'a\\']) expect(() => shellWords(line)).toThrow(ShellWordsError);
  });
});
