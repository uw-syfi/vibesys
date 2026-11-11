import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {reportsOf, searchOf, servedSeries} from './served-points.js';
import {
  bandView,
  boundIsDrawn,
  choiceOf,
  LONG_RUN_MS,
  offersRanges,
  type RangeView,
  rangeChoices,
  type Span,
  WHOLE_RUN,
  windowOf,
} from './windows.js';

const HOUR = 3_600_000;
const start = 1_000_000;
const span = (length: number): Span => ({fromMs: start, toMs: start + length});
const length = fc.integer({min: 1_000, max: 8 * 24 * HOUR});

const rangeView: fc.Arbitrary<RangeView> = fc.oneof(
  fc.constant<RangeView>(WHOLE_RUN),
  fc.constantFrom<RangeView>(
    {kind: 'last', choice: 'hour'},
    {kind: 'last', choice: 'six-hours'},
    {kind: 'last', choice: 'day'},
  ),
  fc
    .tuple(
      fc.integer({min: -HOUR, max: 9 * 24 * HOUR}),
      fc.integer({min: -HOUR, max: 9 * 24 * HOUR}),
    )
    .map(([a, b]): RangeView => ({kind: 'between', fromMs: start + a, toMs: start + b})),
);

describe('the range of a long run', () => {
  it('is offered only for a run longer than six hours, and then always ends in the whole run', () => {
    fc.assert(
      fc.property(length, run => {
        const choices = rangeChoices(span(run));
        expect(offersRanges(span(run))).toBe(run > LONG_RUN_MS);
        if (run <= LONG_RUN_MS) expect(choices).toEqual([]);
        else expect(choices.at(-1)).toBe('all');
      }),
    );
  });

  it('never offers a last-so-long that is as long as the run itself', () => {
    expect(rangeChoices(span(7 * HOUR))).toEqual(['hour', 'six-hours', 'all']);
    expect(rangeChoices(span(30 * HOUR))).toEqual(['hour', 'six-hours', 'day', 'all']);
    expect(rangeChoices(span(5 * HOUR))).toEqual([]);
  });

  it('shows a window inside the run, never empty, and the whole run when it is not long, whatever was chosen', () => {
    fc.assert(
      fc.property(length, rangeView, (run, view) => {
        const shown = windowOf(view, span(run));
        expect(shown.fromMs).toBeGreaterThanOrEqual(start);
        expect(shown.toMs).toBeLessThanOrEqual(start + run);
        expect(shown.toMs).toBeGreaterThan(shown.fromMs - 1);
        if (run <= LONG_RUN_MS) expect(shown).toEqual(span(run));
      }),
      {seed: 61},
    );
  });

  it('keeps "the last hour" as a choice that follows the end of a live run', () => {
    const view: RangeView = {kind: 'last', choice: 'hour'};
    expect(windowOf(view, span(10 * HOUR))).toEqual({
      fromMs: start + 9 * HOUR,
      toMs: start + 10 * HOUR,
    });
    expect(windowOf(view, span(11 * HOUR))).toEqual({
      fromMs: start + 10 * HOUR,
      toMs: start + 11 * HOUR,
    });
    expect(choiceOf(view)).toBe('hour');
    expect(choiceOf({kind: 'between', fromMs: 1, toMs: 2})).toBeNull();
  });
});

describe('what a chart draws of a window', () => {
  it('draws the measurements inside the window, oldest first, and the host’s corners among them', () => {
    fc.assert(
      fc.property(
        servedSeries,
        fc.integer({min: 0, max: 100_000}),
        fc.integer({min: 1, max: 100_000}),
        (series, from, size) => {
          const window = {fromMs: from * 1000, toMs: (from + size) * 1000};
          for (const objective of reportsOf(searchOf(series))) {
            const view = bandView(objective, window);
            expect(view.points).toEqual(
              objective.points.filter(p => p.atMs >= window.fromMs && p.atMs <= window.toMs),
            );
            expect(view.corners).toEqual(view.points.filter(p => p.bestSoFar));
          }
        },
      ),
      {seed: 62},
    );
  });

  it('carries the best standing at the window’s start in, and the line on to its end, one value across the window', () => {
    fc.assert(
      fc.property(
        servedSeries,
        fc.integer({min: 0, max: 100_000}),
        fc.integer({min: 1, max: 100_000}),
        (series, from, size) => {
          const window = {fromMs: from * 1000, toMs: (from + size) * 1000};
          for (const objective of reportsOf(searchOf(series))) {
            const {steps} = bandView(objective, window);
            if (steps.length === 0) continue;
            expect(steps.at(-1)?.x).toBe(window.toMs);
            for (const step of steps) {
              expect(step.x).toBeGreaterThanOrEqual(window.fromMs);
              expect(step.x).toBeLessThanOrEqual(window.toMs);
            }
            steps.forEach((step, at) => {
              if (at > 0) expect(step.x).toBeGreaterThanOrEqual(steps[at - 1]?.x ?? 0);
            });
          }
        },
      ),
      {seed: 63},
    );
  });

  it('spans a value axis that holds every point drawn, with room to spare', () => {
    fc.assert(
      fc.property(servedSeries, series => {
        for (const objective of reportsOf(searchOf(series))) {
          const view = bandView(objective, {fromMs: 0, toMs: 200_000_000});
          for (const p of view.points) {
            expect(p.value).toBeGreaterThan(view.domain[0]);
            expect(p.value).toBeLessThan(view.domain[1]);
          }
        }
      }),
      {seed: 64},
    );
  });

  it('draws a bound only when it is near the data, so a far one never flattens the points', () => {
    expect(boundIsDrawn(48, 120, 160)).toBe(true);
    expect(boundIsDrawn(48, 120, 1600)).toBe(false);
    expect(boundIsDrawn(4800, 11_100, 14_000)).toBe(true);
    expect(boundIsDrawn(4800, 11_100, 15_600)).toBe(false);
    expect(boundIsDrawn(212, 118, 74)).toBe(false);
  });
});
