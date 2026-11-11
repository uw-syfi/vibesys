import {describe, expect, test} from 'bun:test';
import {type BarPlace, fittedTimeTicks, placeBars, type StackedBar} from './index.js';

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

const DAY = 86_400_000;
const bar = (startMs: number, widthMs: number): StackedBar => ({
  id: String(startMs),
  segments: [],
  total: 0,
  title: '',
  startMs,
  widthMs,
});

/** Bars of one width at increasing, uneven moments, with gaps of any length between them. */
function barsOf(next: () => number): {bars: StackedBar[]; fromMs: number; toMs: number} {
  const widthMs = 60_000 * (1 + Math.floor(next() * 600));
  const fromMs = 1_700_000_000_000 + Math.floor(next() * 1e9);
  const bars: StackedBar[] = [];
  let at = fromMs;
  for (let count = 1 + Math.floor(next() * 40); count > 0; count -= 1) {
    bars.push(bar(at, widthMs));
    at += widthMs * (1 + Math.floor(next() * 5));
  }
  return {bars, fromMs, toMs: at + widthMs * Math.floor(next() * 3)};
}

/** Each bar's place is its share of the span; the places grow with time, in the ratio of the times between them. */
function expectProportional(
  bars: readonly StackedBar[],
  places: readonly BarPlace[],
  fromMs: number,
  toMs: number,
): void {
  const first = places[0];
  const origin = bars[0];
  for (const [at, place] of places.entries()) {
    const here = bars[at];
    if (here === undefined) throw new Error('a place for each bar');
    expect(place.left).toBeCloseTo(((here.startMs ?? 0) - fromMs) / (toMs - fromMs), 12);
    expect(place.width).toBeCloseTo((here.widthMs ?? 0) / (toMs - fromMs), 12);
    const before = places[at - 1];
    if (before === undefined || first === undefined || origin === undefined) continue;
    expect(place.left).toBeGreaterThan(before.left);
    expect((place.left - first.left) / ((here.startMs ?? 0) - (origin.startMs ?? 0))).toBeCloseTo(
      1 / (toMs - fromMs),
      15,
    );
  }
}

describe('placing bars on a time axis', () => {
  test('puts each bar at its moment: left edges grow with time and are proportional to it', () => {
    const next = random(11);
    for (let round = 0; round < 200; round += 1) {
      const {bars, fromMs, toMs} = barsOf(next);
      expectProportional(bars, placeBars(bars, fromMs, toMs), fromMs, toMs);
    }
  });

  test('leaves a gap in the data as a gap on the plot, and never lets a bar run into the next', () => {
    const next = random(12);
    for (let round = 0; round < 200; round += 1) {
      const {bars, fromMs, toMs} = barsOf(next);
      const places = placeBars(bars, fromMs, toMs);
      for (let at = 1; at < places.length; at += 1) {
        const [before, here] = [places[at - 1], places[at]];
        const [earlier, later] = [bars[at - 1], bars[at]];
        if (!before || !here || !earlier || !later) continue;
        const gapMs = (later.startMs ?? 0) - ((earlier.startMs ?? 0) + (earlier.widthMs ?? 0));
        expect(here.left - (before.left + before.width)).toBeCloseTo(gapMs / (toMs - fromMs), 12);
        expect(here.left).toBeGreaterThanOrEqual(before.left + before.width - 1e-12);
      }
    }
  });

  test('is the same bar widths for equal durations, however far along the axis they are', () => {
    const places = placeBars([bar(0, DAY), bar(3 * DAY, DAY)], 0, 7 * DAY);
    expect(places[0]?.width).toBeCloseTo(1 / 7, 12);
    expect(places[1]?.width).toBe(places[0]?.width ?? NaN);
    expect(places[1]?.left).toBeCloseTo(3 / 7, 12);
  });
});

const OFFSET = -8 * 3_600_000;

/** A span's day ticks are its clock's midnights, none skipped inside, equally spaced and at most a day apart. */
function expectMidnights(fromMs: number, toMs: number, width: number): void {
  const ticks = fittedTimeTicks(
    fromMs,
    toMs,
    width,
    () => '',
    () => OFFSET,
  );
  const midnights: number[] = [];
  for (let at = Math.ceil((fromMs + OFFSET) / DAY) * DAY - OFFSET; at <= toMs; at += DAY) {
    midnights.push(at);
  }
  // A label too near an end to fit is no tick, but no day in between is ever dropped.
  const [first, last] = [ticks[0]?.at ?? 0, ticks.at(-1)?.at ?? 0];
  const at = ticks.map(tick => tick.at);
  for (const midnight of midnights.filter(each => each >= first && each <= last)) {
    expect(at).toContain(midnight);
  }
  // Equally spaced, and no coarser than a day: a week is never ticked every other day.
  for (let place = 1; place < at.length; place += 1) {
    expect((at[place] ?? 0) - (at[place - 1] ?? 0)).toBe(
      DAY / (at.length > midnights.length ? 2 : 1),
    );
  }
  expect(ticks.length).toBeGreaterThanOrEqual(midnights.length - 2);
}

describe('the ticks of a week of bars', () => {
  test('are at the midnights of the clock, none skipped, at any width that holds them', () => {
    const next = random(13);
    for (let round = 0; round < 100; round += 1) {
      const fromMs = 1_700_000_000_000 + Math.floor(next() * 1e9);
      const toMs = fromMs + (6.2 + next() * 2.6) * DAY;
      expectMidnights(fromMs, toMs, 900 + Math.floor(next() * 1100));
    }
  });
});
