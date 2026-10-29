import {describe, expect, test} from 'bun:test';
import type {NarrationLevel, NarrationLine} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {decodeNarrationLine} from './index.js';

const LEVELS: readonly NarrationLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
const CHARACTERS = ['a', 'Z', ' ', '"', '\\', '{', '}', '\n', 'é', '€', ':', ','];

function words(random: Seeded, max: number): string {
  return Array.from({length: random.int(max)}, () => random.pick(CHARACTERS)).join('');
}

function narrationLine(random: Seeded): NarrationLine {
  return {
    at: '2026-10-15T12:00:00.000000Z',
    level: random.pick(LEVELS),
    logger: words(random, 8),
    thread: 'MainThread',
    message: words(random, 30),
    traceback: random.int(2) === 0 ? null : words(random, 40),
  };
}

describe('decodeNarrationLine', () => {
  test('property: every line the writer encodes decodes to the same record', () => {
    for (const seed of SEEDS) {
      const line = narrationLine(new Seeded(seed));
      expect({seed, record: decodeNarrationLine(JSON.stringify(line))}).toEqual({
        seed,
        record: {kind: 'line', line},
      });
    }
  });

  test('property: any text decodes without throwing, and text that is not a record shows raw', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const encoded = JSON.stringify(narrationLine(random));
      const text = encoded.slice(0, random.int(encoded.length)) + words(random, 5);
      const record = decodeNarrationLine(text);
      if (record.kind === 'raw') expect(record.text).toBe(text);
      else expect(JSON.parse(text)).toMatchObject(record.line);
    }
  });

  test('a record with an unknown key, an unknown level, or a non-object shows raw', () => {
    const line = {
      at: 'x',
      level: 'INFO',
      logger: 'l',
      thread: 't',
      message: 'm',
    };
    for (const text of [
      JSON.stringify({...line, extra: 1}),
      JSON.stringify({...line, level: 'NOTICE'}),
      JSON.stringify({...line, traceback: 3}),
      '[1, 2]',
      'null',
      'plain text',
    ]) {
      expect(decodeNarrationLine(text)).toEqual({kind: 'raw', text});
    }
  });

  test('an absent traceback reads as none', () => {
    const line = {at: 'x', level: 'ERROR' as const, logger: 'l', thread: 't', message: 'm'};
    expect(decodeNarrationLine(JSON.stringify(line))).toEqual({
      kind: 'line',
      line: {...line, traceback: null},
    });
  });
});
