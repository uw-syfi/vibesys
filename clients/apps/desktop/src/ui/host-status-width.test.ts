import {readFileSync} from 'node:fs';
import {describe, expect, it} from '@effect/vitest';
import {REFUSAL_CAUSES} from '../state/index.js';
import {HOST_STATUS_LABELS} from './host-status-words.js';
import {causeWord} from './refusal-words.js';

/**
 * The status column's chips are all as wide as `--chip-even`, so a host whose word changes never
 * moves its neighbours. That holds only while no word is wider than the token: the widest label
 * (a cause's) once drew a chip of its own width beside narrower ones.
 */
const tokens = readFileSync(new URL('../../../../libs/ui/src/tokens.css', import.meta.url), 'utf8');

/** A caption-size glyph is at most this wide on average; a dot, its gap, padding and border add the rest. */
const GLYPH_PX = 5.6;
const CHROME_PX = 8 + 8 + 24 + 2;

describe('the status chip width', () => {
  it('fits the widest word a host status can say', () => {
    const declared = /--chip-even:\s*(\d+)px;/.exec(tokens);
    expect(declared).not.toBeNull();
    const width = Number(declared?.[1]);
    const words = [...HOST_STATUS_LABELS, ...REFUSAL_CAUSES.map(causeWord), 'Stale 59m'];
    for (const word of words) {
      expect(word.length * GLYPH_PX + CHROME_PX, word).toBeLessThanOrEqual(width);
    }
  });
});
