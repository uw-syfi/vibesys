import {describe, expect, test} from 'bun:test';
import {type AxisLabel, placeLabels} from './index.js';

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

function labelsOf(next: () => number): AxisLabel[] {
  const count = 1 + Math.floor(next() * 30);
  const shares = Array.from({length: count}, () => next()).sort((a, b) => a - b);
  return shares.map((share, index) => ({share, text: `Call ${index + 1}`}));
}

const GLYPH = 8;
const GAP = 8;

describe('placeLabels', () => {
  test('never overlaps two labels or leaves the axis, whatever the labels and width', () => {
    const next = random(7);
    for (let round = 0; round < 300; round += 1) {
      const labels = labelsOf(next);
      const width = 40 + next() * 1200;
      const placed = placeLabels(labels, width, GLYPH, GAP);
      let right = Number.NEGATIVE_INFINITY;
      for (const {index, left} of placed) {
        const own = (labels[index]?.text.length ?? 0) * GLYPH;
        expect(left).toBeGreaterThanOrEqual(0);
        if (own <= width) expect(left + own).toBeLessThanOrEqual(width + 1e-6);
        expect(left).toBeGreaterThanOrEqual(right + GAP - 1e-6);
        right = left + own;
      }
      const order = placed.map(each => each.index);
      expect(order).toEqual([...order].sort((a, b) => a - b));
    }
  });

  test('keeps every label on a wide axis, and the first and last on a narrow one', () => {
    const next = random(11);
    for (let round = 0; round < 200; round += 1) {
      const labels = labelsOf(next);
      expect(placeLabels(labels, 1e12, GLYPH, GAP)).toHaveLength(labels.length);
      const narrow = placeLabels(labels, 300, GLYPH, GAP).map(each => each.index);
      expect(narrow[0]).toBe(0);
      const spread = (labels.at(-1)?.share ?? 0) - (labels[0]?.share ?? 0);
      if (spread >= 0.5) expect(narrow.at(-1)).toBe(labels.length - 1);
    }
  });
});
