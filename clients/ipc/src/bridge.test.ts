import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/core-state/testing';
import {type PageToMain, parsePageMessage} from './index.js';

/** An arbitrary JSON-like value, the shape of anything a page could post on its port. */
function arbitrary(random: Seeded, depth = 0): unknown {
  const kinds = depth > 2 ? 4 : 6;
  switch (random.int(kinds)) {
    case 0:
      return null;
    case 1:
      return random.int(1000) - 500;
    case 2:
      return random.pick(['', 'send', 'close', 'open', 'message', 'x']);
    case 3:
      return random.int(2) === 0;
    case 4:
      return Array.from({length: random.int(3)}, () => arbitrary(random, depth + 1));
    default: {
      const value: Record<string, unknown> = {};
      for (const key of ['type', 'data', 'extra'].filter(() => random.int(2) === 0)) {
        value[key] = arbitrary(random, depth + 1);
      }
      return value;
    }
  }
}

describe('parsePageMessage', () => {
  test('accepts exactly a send with string data, or a close', () => {
    expect(parsePageMessage({type: 'send', data: 'line'})).toEqual({type: 'send', data: 'line'});
    expect(parsePageMessage({type: 'close'})).toEqual({type: 'close'});
    expect(parsePageMessage({type: 'send'})).toBeNull();
    expect(parsePageMessage({type: 'open'})).toBeNull();
  });

  test('never returns anything but a well-formed page message', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      for (let index = 0; index < 200; index += 1) {
        const parsed: PageToMain | null = parsePageMessage(arbitrary(random));
        if (parsed === null) continue;
        if (parsed.type === 'send') expect(typeof parsed.data).toBe('string');
        else expect(parsed).toEqual({type: 'close'});
      }
    }
  });
});
