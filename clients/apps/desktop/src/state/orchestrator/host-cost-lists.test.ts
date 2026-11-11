import {describe, expect, it} from '@effect/vitest';
import {Harness, RunStatus} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {CostUsage, SpendBucketView, SpendSeriesView} from './cost.js';
import type {HostRunCostView} from './host-cost.js';
import {
  bucketsInRange,
  HOST_ORDER,
  nextRunSort,
  type RunSortKey,
  rangeOf,
  rangesOf,
  sortRuns,
  spendSpanMs,
} from './host-cost-lists.js';

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const START = Date.UTC(2026, 10, 1);
const MODEL = {harness: Harness.CODEX, model: 'model-a'};

/** A series of equal buckets, those at `spent` places holding a dollar. */
function seriesOf(count: number, bucketMs: number, spent: ReadonlySet<number>): SpendSeriesView {
  const buckets: SpendBucketView[] = Array.from({length: count}, (_, at) => ({
    startMs: START + at * bucketMs,
    costUsd: spent.has(at) ? 1 : 0,
    byModel: spent.has(at) ? [{model: MODEL, costUsd: 1}] : [],
  }));
  return {startMs: START, bucketMs, buckets};
}

const series = fc
  .record({
    count: fc.integer({min: 1, max: 64}),
    bucketMs: fc.constantFrom(60_000, HOUR, 3 * HOUR, DAY),
    spent: fc.array(fc.nat({max: 63}), {maxLength: 20}),
  })
  .map(({count, bucketMs, spent}) =>
    seriesOf(count, bucketMs, new Set(spent.filter(at => at < count))),
  );

const asOf = (view: SpendSeriesView): number => START + view.buckets.length * view.bucketMs;

/** Runs sorted by turns are in the direction asked, the host's order kept among those that tie. */
function expectTurnsOrdered(
  list: readonly HostRunCostView[],
  dir: 'ascending' | 'descending',
): void {
  const sorted = sortRuns(list, {key: 'turns', direction: dir});
  for (let at = 1; at < sorted.length; at += 1) {
    const [before, after] = [sorted[at - 1], sorted[at]];
    if (before === undefined || after === undefined) continue;
    const gap = dir === 'ascending' ? after.turns - before.turns : before.turns - after.turns;
    expect(gap).toBeGreaterThanOrEqual(0);
    if (gap === 0) expect(list.indexOf(before)).toBeLessThan(list.indexOf(after));
  }
}

describe('the ranges of the spend chart', () => {
  it('are offered only for spend that spans more than a day, and always end in the whole span', () => {
    fc.assert(
      fc.property(series, view => {
        const ranges = rangesOf(view);
        if (spendSpanMs(view) <= DAY) expect(ranges).toEqual([]);
        else expect(ranges.at(-1)).toBe('all');
      }),
    );
  });

  it('offer a week only for spend that spans more than one, and a day only for more than a day', () => {
    expect(rangesOf(seriesOf(7, DAY, new Set([0, 6])))).toEqual(['1d', 'all']);
    expect(rangesOf(seriesOf(10, DAY, new Set([0, 9])))).toEqual(['1d', '7d', 'all']);
    expect(rangesOf(seriesOf(24, HOUR, new Set([0, 23])))).toEqual([]);
    expect(rangesOf(seriesOf(3, DAY, new Set()))).toEqual([]);
  });

  it('fall back to the whole span for a choice that is not offered', () => {
    const short = seriesOf(5, HOUR, new Set([1]));
    expect(rangeOf(short, '7d')).toBe('all');
    expect(rangeOf(seriesOf(10, DAY, new Set([0, 9])), '7d')).toBe('7d');
  });

  it('fit the data: "all" runs from the first bucket with spend to the last, and nothing else', () => {
    fc.assert(
      fc.property(series, view => {
        const shown = bucketsInRange(view, asOf(view), 'all');
        const spent = view.buckets.filter(bucket => bucket.costUsd > 0);
        if (spent.length === 0) {
          expect(shown).toEqual([]);
          return;
        }
        expect(shown[0]).toBe(spent[0]);
        expect(shown.at(-1)).toBe(spent.at(-1));
        expect(shown.length * view.bucketMs).toBe(spendSpanMs(view));
      }),
    );
  });

  it("draw the host's buckets whole, a contiguous run of them, never one made up or changed", () => {
    fc.assert(
      fc.property(series, fc.constantFrom('1d', '7d', 'all'), (view, range) => {
        const shown = bucketsInRange(view, asOf(view), range);
        const at = shown.length === 0 ? 0 : view.buckets.indexOf(shown[0] as SpendBucketView);
        expect(shown).toEqual(view.buckets.slice(at, at + shown.length));
        for (const bucket of shown) expect(view.buckets).toContain(bucket);
      }),
    );
  });

  it('count a day or a week back from the instant the view is worked out at', () => {
    const week = seriesOf(10, DAY, new Set([0, 9]));
    const end = asOf(week);
    expect(bucketsInRange(week, end, '1d').map(bucket => bucket.startMs)).toEqual([
      START + 9 * DAY,
    ]);
    expect(bucketsInRange(week, end, '7d')).toHaveLength(7);
    // A view worked out a day and an hour later has none of those buckets in its last day: the
    // range counts from the host's instant, so it is the same for any viewer at any time.
    expect(bucketsInRange(week, end + 25 * HOUR, '1d')).toHaveLength(0);
  });
});

const usage = (costUsd: number | null): CostUsage => ({
  inputTokens: null,
  cacheReadTokens: null,
  cacheCreationTokens: null,
  outputTokens: null,
  reasoningTokens: null,
  costUsd,
});

const run = fc
  .record({
    id: fc.stringMatching(/^[a-z]{1,6}$/),
    name: fc.stringMatching(/^[A-Za-z ]{1,12}$/),
    turns: fc.nat({max: 500}),
    tokens: fc.nat({max: 10 ** 9}),
    cost: fc.option(fc.integer({min: 0, max: 5000}), {nil: null}),
    budget: fc.option(fc.integer({min: 1, max: 5000}), {nil: null}),
  })
  .map(
    (fields): HostRunCostView => ({
      runId: fields.id,
      name: fields.name,
      status: RunStatus.ENDED,
      maxCostUsd: fields.budget,
      turns: fields.turns,
      unpricedTurns: 0,
      usage: usage(fields.cost),
      totalTokens: fields.tokens,
    }),
  );
const runs = fc.array(run, {maxLength: 20});
const KEYS = ['run', 'turns', 'tokens', 'cost', 'budget'] as const satisfies readonly RunSortKey[];
const direction = fc.constantFrom('ascending', 'descending');

describe('the order of the runs', () => {
  it("is a reordering of the host's list: no run is lost, added or changed", () => {
    fc.assert(
      fc.property(runs, fc.constantFrom(...KEYS), direction, (list, key, dir) => {
        const sorted = sortRuns(list, {key, direction: dir});
        expect(sorted).toHaveLength(list.length);
        for (const each of list) expect(sorted).toContain(each);
      }),
    );
  });

  it('puts a run with no value in the column last, whichever way the column is ordered', () => {
    fc.assert(
      fc.property(runs, direction, (list, dir) => {
        for (const key of ['cost', 'budget'] as const) {
          const sorted = sortRuns(list, {key, direction: dir});
          const value = (each: HostRunCostView) =>
            key === 'cost' ? each.usage.costUsd : each.maxCostUsd;
          const firstMissing = sorted.findIndex(each => value(each) === null);
          if (firstMissing >= 0) {
            for (const after of sorted.slice(firstMissing)) expect(value(after)).toBeNull();
          }
        }
      }),
    );
  });

  it("orders a column by its figure, and keeps the host's order among runs that tie", () => {
    fc.assert(fc.property(runs, direction, expectTurnsOrdered));
  });

  it("is the host's own ranking when the runs are ranked by cost, most costly first", () => {
    fc.assert(
      fc.property(runs, list => {
        const ranked = sortRuns(list, HOST_ORDER);
        expect(sortRuns(ranked, HOST_ORDER)).toEqual(ranked);
      }),
    );
  });

  it('turns a column over when it is pressed again and starts another in its own first order', () => {
    expect(nextRunSort(HOST_ORDER, 'cost')).toEqual({key: 'cost', direction: 'ascending'});
    expect(nextRunSort(HOST_ORDER, 'run')).toEqual({key: 'run', direction: 'ascending'});
    expect(nextRunSort({key: 'run', direction: 'ascending'}, 'turns')).toEqual({
      key: 'turns',
      direction: 'descending',
    });
  });
});
