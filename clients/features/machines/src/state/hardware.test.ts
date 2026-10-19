import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {gpuLine, gpuName} from './hardware.js';

const BASES = ['h100', 'mi300x', 'a100_80gb', 'mi250x', 'l40s', 'gb200'] as const;
const VENDORS = ['', 'nvidia_', 'NVIDIA-', 'amd_', 'AMD '] as const;

/** A raw model id as some source spells it: a vendor prefix, a case, the model. */
function rawId(random: Seeded): {raw: string; base: string} {
  const base = random.pick(BASES);
  const spelled = random.bool() ? base : base.toUpperCase();
  return {raw: `${random.pick(VENDORS)}${spelled}`, base};
}

describe('gpuName', () => {
  test('writes the examples the page shows', () => {
    expect(gpuName('nvidia_h100')).toBe('H100');
    expect(gpuName('mi300x')).toBe('MI300X');
    expect(gpuName('H100')).toBe('H100');
  });

  test('property: every spelling of a model reads the same, in capitals, with no raw id left', () => {
    for (const seed of SEEDS.slice(0, 200)) {
      const random = new Seeded(seed);
      const {raw, base} = rawId(random);
      const name = gpuName(raw);
      expect(name).toBe(gpuName(base));
      expect(name).toBe(name.toUpperCase());
      expect(name).not.toMatch(/[_\s]$|^nvidia|^amd/i);
      expect(name).not.toContain('_');
      expect(gpuName(name)).toBe(name);
    }
  });
});

describe('gpuLine', () => {
  test('property: a count above one leads the names, one GPU is its name alone, none is empty', () => {
    for (const seed of SEEDS.slice(0, 200)) {
      const random = new Seeded(seed);
      const {raw} = rawId(random);
      const count = random.int(9);
      const line = gpuLine(count, [raw]);
      if (count > 1) expect(line).toBe(`${count}× ${gpuName(raw)}`);
      else expect(line).toBe(gpuName(raw));
      expect(gpuLine(count, [])).toBe('');
    }
  });
});
