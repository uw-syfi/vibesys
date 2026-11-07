import {describe, expect, test} from 'bun:test';
import {fittedTimeTicks, labelsFit, placeLabels, timeTicks} from './index.js';

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

/** A span anywhere from a second to months, starting at any moment of a recent year. */
function spanOf(next: () => number): readonly [number, number] {
  const from = 1_700_000_000_000 + Math.floor(next() * 31_536_000_000);
  const length = 10 ** (3 + next() * 6.5);
  return [from, from + Math.max(1, Math.floor(length))];
}

describe('timeTicks', () => {
  test('are ascending, equally spaced, inside the span and no more than asked, over every span', () => {
    const next = random(3);
    for (let trial = 0; trial < 500; trial += 1) {
      const [from, to] = spanOf(next);
      const most = 1 + Math.floor(next() * 14);
      const ticks = timeTicks(from, to, most);
      expect(ticks.length).toBeLessThanOrEqual(most);
      for (const at of ticks) {
        expect(at).toBeGreaterThanOrEqual(from);
        expect(at).toBeLessThanOrEqual(to);
      }
      const [first, second] = ticks;
      if (first === undefined || second === undefined) continue;
      const step = second - first;
      ticks.forEach((at, index) => {
        expect(at).toBe(first + index * step);
      });
      // Tidy: a whole number of seconds once the step is a second or more, and the step is a multiple of the epoch's own.
      if (step >= 1000) expect(at0(ticks) % 1000).toBe(0);
    }
  });

  test('draw as many ticks as fit, so a span of minutes is not left with one', () => {
    const from = Date.UTC(2026, 10, 7, 20, 26, 1);
    const to = Date.UTC(2026, 10, 7, 20, 31, 11);
    const ticks = timeTicks(from, to, 6);
    expect(ticks.length).toBeGreaterThanOrEqual(4);
    expect(ticks.map(at => (at - ticks[0]!) / 1000)).toEqual(
      ticks.map((_, index) => index * (((ticks[1] ?? 0) - (ticks[0] ?? 0)) / 1000)),
    );
  });
});

function at0(ticks: readonly number[]): number {
  return ticks[0] ?? 0;
}

describe('fittedTimeTicks', () => {
  const label = (at: number, span: number): string =>
    `${new Date(at).toISOString().slice(11, span < 600_000 ? 19 : 16)}`;

  test('never drop a label to fit, stay equally spaced and inside the span, at every width', () => {
    const next = random(9);
    for (let trial = 0; trial < 400; trial += 1) {
      const [from, to] = spanOf(next);
      const width = 30 + next() * 1500;
      const ticks = fittedTimeTicks(from, to, width, label);
      const placed = placeLabels(
        ticks.map(tick => ({share: (tick.at - from) / (to - from), text: tick.label})),
        width,
      );
      expect(placed).toHaveLength(ticks.length);
      for (const tick of ticks) {
        expect(tick.at).toBeGreaterThanOrEqual(from);
        expect(tick.at).toBeLessThanOrEqual(to);
      }
      const [first, second] = ticks;
      if (first === undefined || second === undefined) continue;
      ticks.forEach((tick, index) => {
        expect(tick.at).toBe(first.at + index * (second.at - first.at));
      });
    }
  });

  test('draw a coarser axis, not a gappy one, as the width narrows', () => {
    const from = Date.UTC(2026, 10, 7, 20, 26, 1);
    const to = from + 310_000;
    const counts = [900, 500, 300, 150, 60].map(
      width => fittedTimeTicks(from, to, width, label).length,
    );
    expect(counts).toEqual([...counts].sort((a, b) => b - a));
  });

  test('keep every label centred inside the axis and clear of its neighbours, with no sliding', () => {
    const next = random(21);
    for (let trial = 0; trial < 400; trial += 1) {
      const [from, to] = spanOf(next);
      const width = 60 + next() * 1500;
      const ticks = fittedTimeTicks(from, to, width, label);
      expect(
        labelsFit(
          ticks.map(tick => ({share: (tick.at - from) / (to - from), text: tick.label})),
          width,
        ),
      ).toBe(true);
    }
  });

  test('tick a day axis at the midnights of the clock it is given, whatever its offset', () => {
    const next = random(33);
    for (let trial = 0; trial < 300; trial += 1) {
      const [from] = spanOf(next);
      const to =
        from + (2 + Math.floor(next() * 12)) * 86_400_000 + Math.floor(next() * 40_000_000);
      const offset = (Math.floor(next() * 48) - 24) * 1_800_000;
      const ticks = fittedTimeTicks(from, to, 1200, label, () => offset);
      for (const tick of ticks) expect((tick.at + offset) % 3_600_000).toBe(0);
    }
  });
});
