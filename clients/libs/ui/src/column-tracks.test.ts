import {describe, expect, test} from 'bun:test';
import {type ColumnSize, columnTier, type TrackedColumn, widthToShowAll} from './index.js';

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

const SIZES: readonly ColumnSize[] = [
  'fill',
  'shrink',
  'wide',
  'medium',
  'short',
  'compact',
  'dot',
];

function columnsOf(next: () => number): TrackedColumn[] {
  const count = 1 + Math.floor(next() * 9);
  return Array.from({length: count}, (_, at) => ({
    size: at === 0 ? 'fill' : (SIZES[Math.floor(next() * SIZES.length)] ?? 'short'),
    optional: at > 0 && next() < 0.5,
  }));
}

describe('columnTier', () => {
  test('shows every column from the width they need to be read, and not before', () => {
    const next = random(11);
    for (let round = 0; round < 300; round += 1) {
      const columns = columnsOf(next);
      const needed = widthToShowAll(columns);
      expect(columnTier(needed, columns, false)).toBe('wide');
      expect(columnTier(needed + next() * 2000, columns, true)).toBe('wide');
      expect(columnTier(needed - 1, columns, false)).toBe('narrow');
    }
  });

  test('is wide wherever the width is not measured', () => {
    const next = random(5);
    for (let round = 0; round < 50; round += 1) {
      expect(columnTier(null, columnsOf(next), next() < 0.5)).toBe('wide');
    }
  });

  test('never takes a narrower tier at a greater width', () => {
    const rank = {wide: 0, narrow: 1, tight: 2} as const;
    const next = random(23);
    for (let round = 0; round < 200; round += 1) {
      const columns = columnsOf(next);
      const crowded = next() < 0.5;
      let last = 3;
      for (let width = 0; width <= 2400; width += 40) {
        const now = rank[columnTier(width, columns, crowded)];
        expect(now).toBeLessThanOrEqual(last);
        last = now;
      }
    }
  });

  test('takes the tight sizes only for a crowded list in a narrow pane', () => {
    const next = random(3);
    for (let round = 0; round < 200; round += 1) {
      const columns = columnsOf(next);
      const width = next() * 2400;
      if (columnTier(width, columns, false) === 'tight')
        throw new Error('an uncrowded list is tight');
      if (columnTier(width, columns, true) === 'tight') expect(width).toBeLessThanOrEqual(560);
    }
  });

  test('a column that shrinks costs less to show than one at a figure width', () => {
    const base: TrackedColumn[] = [{size: 'fill'}, {size: 'short'}];
    const shrunk: TrackedColumn[] = [{size: 'fill'}, {size: 'shrink'}];
    expect(widthToShowAll(shrunk)).toBeLessThan(widthToShowAll(base));
  });
});
