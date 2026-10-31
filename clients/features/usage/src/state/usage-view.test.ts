import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {periodEdges} from './edges.js';
import {dollars, tokens} from './format.js';
import {claimSources, type HostReply, mergeReplies} from './merge.js';
import {PROVIDERS, repliesFor} from './testing/generate.js';
import {type UsageSourceChoice, type UsageTab, usageView, type ViewFilters} from './usage-view.js';

const NOW = Date.UTC(2026, 9, 30, 15, 0, 0);
const EDGES = periodEdges('7d', NOW, 'UTC');
const N = EDGES.edges.length - 1;

function viewOf(
  replies: readonly HostReply[],
  filters: Partial<ViewFilters> = {},
  pricing = replies.map(each => each.summary.pricing),
) {
  const merged = mergeReplies(claimSources(replies), N);
  return usageView(
    merged,
    {tab: 'cost', source: 'all', hidden: new Set(), ...filters},
    {
      names: [],
      edges: EDGES,
      pricing,
      unreadablePrices: false,
      olderRunsUnread: false,
      overridden: new Set(),
    },
  );
}

/** The series of `replies` the source and the hidden providers leave. */
function shownSeries(
  replies: readonly HostReply[],
  source: UsageSourceChoice,
  hidden: ReadonlySet<string>,
) {
  return replies
    .flatMap(reply => reply.summary.series)
    .filter(
      series => (source === 'all' || series.origin === source) && !hidden.has(series.provider),
    );
}

/** What the raw columns say the shown rows add up to, summed without the folds under test. */
function oracle(
  replies: readonly HostReply[],
  tab: UsageTab,
  source: UsageSourceChoice,
  hidden: ReadonlySet<string>,
): number {
  const columns = shownSeries(replies, source, hidden).flatMap(series =>
    tab === 'cost'
      ? [series.estimateUsd]
      : [series.cached, series.cacheWrite, series.uncached, series.output],
  );
  return columns.flat().reduce((a, b) => a + b, 0);
}

const figure = (tab: UsageTab, value: number): string =>
  tab === 'cost' ? dollars(value) : tokens(value);

describe('usageView', () => {
  test('property: the hero is what the shown rows add up to, under any tab, source and hidden provider', () => {
    for (const seed of SEEDS.slice(0, 150)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 1 + rng.int(3));
      const tab = rng.pick(['cost', 'tokens'] as const);
      const source = rng.pick(['all', 'run', 'outside'] as const);
      const hidden = new Set(PROVIDERS.filter(() => rng.int(4) === 0));
      const view = viewOf(replies, {tab, source, hidden});
      expect(view.hero.figure, `seed ${seed}`).toBe(
        figure(tab, oracle(replies, tab, source, hidden)),
      );
      // The chart draws the same figure: its layers add up to the hero.
      const drawn = view.chart.layers.flatMap(layer => layer.values).reduce((a, b) => a + b, 0);
      expect(figure(tab, drawn), `seed ${seed}`).toBe(view.hero.figure);
    }
  });

  test('property: the runs and the outside use partition the whole', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 1 + rng.int(3));
      for (const tab of ['cost', 'tokens'] as const) {
        const whole = oracle(replies, tab, 'all', new Set());
        const parts =
          oracle(replies, tab, 'run', new Set()) + oracle(replies, tab, 'outside', new Set());
        expect(parts, `seed ${seed}`).toBe(whole);
        expect(viewOf(replies, {tab}).hero.figure).toBe(figure(tab, whole));
      }
    }
  });

  test('property: hiding a provider takes out exactly its rows, and shows it again with the same figure', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 1 + rng.int(2));
      const provider = rng.pick(PROVIDERS);
      const shown = viewOf(replies);
      const without = viewOf(replies, {hidden: new Set([provider])});
      const row = shown.providers.find(each => each.provider === provider);
      const own = oracle(
        replies,
        'cost',
        'all',
        new Set(PROVIDERS.filter(each => each !== provider)),
      );
      if (row !== undefined) {
        // What a person reads for the provider: its dollars, or Unpriced when no price covers any
        // of what it used (worked out from the raw series, not from the view).
        const series = replies
          .flatMap(reply => reply.summary.series)
          .filter(each => each.provider === provider);
        const used = series
          .flatMap(each => [each.cached, each.cacheWrite, each.uncached, each.output])
          .flat();
        const covered = series
          .filter(each => each.source !== 'unpriced')
          .flatMap(each => [each.cached, each.cacheWrite, each.uncached, each.output])
          .flat();
        const none = used.some(count => count > 0) && !covered.some(count => count > 0);
        expect(row.figure, `seed ${seed}`).toBe(none ? 'Unpriced' : dollars(own));
      }
      // The row stays so it can be turned on, with no share while hidden.
      expect(without.providers.find(each => each.provider === provider)?.hidden ?? true).toBe(true);
      expect(without.hero.figure, `seed ${seed}`).toBe(
        dollars(oracle(replies, 'cost', 'all', new Set([provider]))),
      );
    }
  });

  test('property: the shares of the shown providers add up to the whole', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 1 + rng.int(2));
      const view = viewOf(replies);
      const total = view.providers.reduce((sum, row) => sum + row.share, 0);
      if (oracle(replies, 'cost', 'all', new Set()) > 0)
        expect(total, `seed ${seed}`).toBeCloseTo(1, 9);
      else expect(total).toBe(0);
    }
  });

  test('property: tokens no price covers are carried as a chip, never dropped', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const replies = repliesFor(rng, N, 1 + rng.int(2));
      const view = viewOf(replies, {tab: 'tokens'});
      const unpriced = replies
        .flatMap(reply => reply.summary.series)
        .filter(series => series.source === 'unpriced')
        .flatMap(series => [series.cached, series.cacheWrite, series.uncached, series.output])
        .flat()
        .reduce((a, b) => a + b, 0);
      const total = oracle(replies, 'tokens', 'all', new Set());
      const chip = view.chips.find(each => each.key === 'unpriced');
      expect(chip !== undefined, `seed ${seed}`).toBe(unpriced > 0 && total > 0);
    }
  });

  test('every bucket gets a row in the hours and days, in the order of the edges', () => {
    const view = viewOf(repliesFor(new Seeded(7), N, 2));
    expect(view.times).toHaveLength(N);
    expect(view.chart.labels).toEqual(view.times.map(row => row.label));
  });

  test('a price list that is not fresh, and prices that cannot be read, are chips', () => {
    const replies = repliesFor(new Seeded(3), N, 1);
    const stale = replies.map(reply => ({
      ...reply.summary.pricing,
      status: 'bundled' as const,
      checkedOn: '2026-10-01',
    }));
    // The chip says which day the prices are of, not only that they are bundled.
    expect(viewOf(replies, {}, stale).chips.map(chip => chip.label)).toContain(
      'Prices of 2026-10-01',
    );
    const merged = mergeReplies(claimSources(replies), N);
    const unreadable = usageView(
      merged,
      {tab: 'cost', source: 'all', hidden: new Set()},
      {
        names: [],
        edges: EDGES,
        pricing: [],
        unreadablePrices: true,
        olderRunsUnread: false,
        overridden: new Set(),
      },
    );
    expect(unreadable.chips.map(chip => chip.label)).toContain('Prices unreadable');
  });
});

describe('what no price covers', () => {
  /** One host that ran only `source` series, in the tokens and dollars the contract gives such a row. */
  function onlyOf(source: 'unpriced' | 'model', seed: number) {
    const replies = repliesFor(new Seeded(seed), N, 1).map(reply => ({
      ...reply,
      summary: {
        ...reply.summary,
        series: reply.summary.series.map(series => ({
          ...series,
          origin: 'run' as const,
          usageSource: 0,
          provider: 'claude',
          model: 'model-a',
          source,
          estimateUsd: series.estimateUsd.map(each => (source === 'unpriced' ? 0 : each + 1)),
        })),
      },
    }));
    return viewOf(replies);
  }

  test('property: a model and a provider no price covers read Unpriced, never a zero, and are not drawn', () => {
    for (const seed of SEEDS.slice(0, 80)) {
      const view = onlyOf('unpriced', seed);
      if (view.models.length === 0) continue;
      const used = view.models.some(model => model.tokens > 0);
      if (!used) continue;
      for (const model of view.models.filter(each => each.tokens > 0)) {
        expect(model.cost, `seed ${seed}`).toBe('Unpriced');
        expect(model.perMillion, `seed ${seed}`).toBeNull();
      }
      expect(view.providers.map(row => row.figure)).toEqual(['Unpriced']);
      // No layer: a line at zero would say the provider was free.
      expect(view.chart.layers, `seed ${seed}`).toEqual([]);
    }
  });

  test('property: a priced model reads dollars and is drawn', () => {
    for (const seed of SEEDS.slice(0, 80)) {
      const view = onlyOf('model', seed);
      for (const model of view.models.filter(each => each.tokens > 0)) {
        expect(model.cost, `seed ${seed}`).toMatch(/^\$/);
      }
      if (view.providers.length > 0 && view.models.some(each => each.tokens > 0)) {
        expect(view.chart.layers.length, `seed ${seed}`).toBe(1);
      }
    }
  });
});

describe('the tokens tab', () => {
  test('property: carries no tile of dollars', () => {
    for (const seed of SEEDS.slice(0, 80)) {
      const replies = repliesFor(new Seeded(seed), N, 1 + new Seeded(seed).int(2));
      const names = viewOf(replies, {tab: 'tokens'}).tiles.map(tile => tile.name);
      expect(names, `seed ${seed}`).not.toContain('Cache savings');
      expect(viewOf(replies, {tab: 'cost'}).tiles.map(tile => tile.name)).toContain(
        'Cache savings',
      );
    }
  });
});
