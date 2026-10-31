import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {plainName} from './format.js';

describe('plainName', () => {
  test('words an id the catalogue has no name for', () => {
    expect(plainName('claude-sonnet-5-5')).toBe('Claude Sonnet 5.5');
    expect(plainName('claude')).toBe('Claude');
    expect(plainName('  Opus-4-5-20251101[1m] ')).toBe('Opus 4.5');
    expect(plainName('vendor/gpt-5-codex')).toBe('Vendor Gpt 5 Codex');
    expect(plainName('')).toBe('');
  });

  test('property: never an id with a dash or an underscore in it, and every letter survives', () => {
    const words = ['claude', 'opus', 'gpt', 'mini', 'next', 'codex', 'sol'];
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const parts = Array.from({length: 1 + rng.int(4)}, () => rng.pick(words));
      if (rng.bool()) parts.push(`${rng.int(9) + 1}`, `${rng.int(9)}`);
      const id = parts.join(rng.pick(['-', '_']));
      const named = plainName(id);
      expect(named, `${id}`).not.toMatch(/[-_]/);
      expect(named.replace(/[ .]/g, '').toLowerCase(), `${id}`).toBe(parts.join('').toLowerCase());
    }
  });
});
