import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {type CostUsage, HOST_ORDER, type HostCostView, type SpendRange} from '../../state/index.js';
import {COST_CASES, type CostCase, costCaseView} from '../../testing/orchestrator/index.js';
import {COST_OVERVIEW_WORDS} from '../test-display-clock.js';
import {hostCostModel, totalFigures} from './cost-overview-model.js';

type Chart = NonNullable<ReturnType<typeof model>['chart']>;

const model = (view: HostCostView, range: SpendRange = 'all') =>
  hostCostModel('host-a', view, COST_OVERVIEW_WORDS, {range, sort: HOST_ORDER});

const views = Object.fromEntries(COST_CASES.map(name => [name, costCaseView(name)])) as Record<
  CostCase,
  HostCostView
>;

const usage = (fields: Partial<CostUsage>): CostUsage => ({
  inputTokens: null,
  cacheReadTokens: null,
  cacheCreationTokens: null,
  outputTokens: null,
  reasoningTokens: null,
  costUsd: null,
  ...fields,
});

/** Every bar stands at its own moment and lasts a bucket; the bars are in time order and the axis holds them. */
function expectBarsAtTheirMoments(chart: Chart): void {
  const starts = chart.bars.map(bar => bar.startMs ?? Number.NaN);
  expect(starts).toEqual([...starts].sort((a, b) => a - b));
  expect(new Set(starts).size).toBe(starts.length);
  for (const bar of chart.bars) expect(bar.widthMs).toBe(chart.bucketMs);
  expect(chart.domain.fromMs).toBe(starts[0]);
  expect(chart.domain.toMs).toBeGreaterThanOrEqual((starts.at(-1) ?? 0) + chart.bucketMs);
  // Gaps in time between bars are whole buckets: nothing is spaced by its place in the list.
  for (const [at, start] of starts.entries()) {
    if (at > 0) expect((start - (starts[at - 1] ?? 0)) % chart.bucketMs).toBe(0);
  }
}

describe('the figures over the hosts', () => {
  it('say what a host that has served no view leaves out: the spend is then a floor', () => {
    const one = views.week;
    expect(totalFigures([one], 1).floor).toBe(true);
    expect(totalFigures([{...one, unpricedTurns: 0}], 1).floor).toBe(false);
    expect(totalFigures([{...one, unpricedTurns: 0}], 2).floor).toBe(true);
    expect(totalFigures([{...one, unpricedTurns: 0}], 2).complete).toBe(false);
  });

  it('count runs and turns with the right noun, and say nothing of unpriced turns when there are none', () => {
    const short = totalFigures([views.short], 1);
    expect(short.runs).toEqual({count: '1', what: 'run'});
    expect(short.turns).toEqual({count: '9', what: 'turns'});
    expect(short.unpriced).toEqual({count: '4', what: 'turns unpriced'});
    expect(totalFigures([{...views.short, unpricedTurns: 1}], 1).unpriced?.what).toBe(
      'turn unpriced',
    );
    expect(totalFigures([{...views.short, unpricedTurns: 0}], 1).unpriced).toBeNull();
  });

  it('write no spend for a host with no priced turn, and no last hour for one that served none', () => {
    const nothing = {...views.none, lastHourUsd: null};
    const figures = totalFigures([nothing], 1);
    expect(figures.figure).toBeNull();
    expect(figures.lastHour).toBeNull();
    expect(totalFigures([], 1).figure).toBeNull();
  });
});

describe('the screen model of a host', () => {
  it('has no spend yet when no turn has finished, and never writes a zero for what is missing', () => {
    const none = model(views.none);
    expect(none.state).toBe('empty');
    expect(none.chart).toBeNull();
    expect(none.ranges).toEqual([]);
    expect(none.unpricedNote).toBeNull();
    const [only] = none.runs;
    expect(only?.turns).toBe('0');
    expect(only?.cost.text).toBe('—');
    expect(only?.tokens).toEqual({input: '—', cached: '—', output: '—'});
  });

  it('is its own frame before any view is served: every list empty, in the same shape', () => {
    const served = model(views.short);
    expect(Object.keys(model(views.none)).sort()).toEqual(Object.keys(served).sort());
  });

  it('marks a cost with unpriced turns behind it as a floor, with their count on hover', () => {
    const short = model(views.short);
    const floors = short.types.filter(line => line.cost.text.startsWith('≥'));
    expect(floors.length).toBeGreaterThan(0);
    for (const line of floors)
      expect(line.cost.hint).toMatch(/turns? reported tokens and no price/);
    expect(short.unpricedNote).toMatch(/^≥ the cost leaves out 4 turns/);
    expect(short.runs[0]?.cost.text.startsWith('≥')).toBe(true);
  });

  it('says unknown, never zero, for a cost no turn priced and for tokens nobody reported', () => {
    const view = views.short;
    const run = view.runs[0];
    if (run === undefined) throw new Error('the case has a run');
    const unpriced = {
      ...view,
      runs: [{...run, usage: usage({outputTokens: 0}), unpricedTurns: run.turns}],
      models: view.models.map(each => ({...each, usage: usage({})})),
    };
    const shown = model(unpriced);
    expect(shown.runs[0]?.cost.text).toBe('unknown');
    expect(shown.runs[0]?.tokens).toEqual({input: 'unknown', cached: 'unknown', output: '0'});
    expect(shown.models.every(line => line.cost.text === 'unknown')).toBe(true);
  });

  it("splits the tokens: the input with the cache reads' share of it, and the output apart", () => {
    fc.assert(
      fc.property(
        fc.nat({max: 10 ** 7}),
        fc.nat({max: 10 ** 7}),
        fc.nat({max: 10 ** 6}),
        (fresh, cached, output) => {
          const view = views.short;
          const run = view.runs[0];
          if (run === undefined) throw new Error('the case has a run');
          const split = model({
            ...view,
            runs: [
              {
                ...run,
                usage: usage({
                  inputTokens: fresh,
                  cacheReadTokens: cached,
                  outputTokens: output,
                  costUsd: 1,
                }),
              },
            ],
          }).runs[0]?.tokens;
          const input = fresh + cached;
          expect(split?.input).not.toBe('unknown');
          if (input === 0) expect(split?.cached).toBe('—');
          else if (cached === 0) expect(split?.cached).toBe('0%');
          else expect(split?.cached).toMatch(/^(<1|\d{1,3})%$/);
        },
      ),
    );
  });

  it('draws the week the data spans, with a range of the last day that draws fewer bars', () => {
    const all = model(views.week);
    expect(all.ranges.map(range => range.value)).toEqual(['1d', 'all']);
    const day = model(views.week, '1d');
    expect(day.range).toBe('1d');
    expect((day.chart?.bars.length ?? 0) < (all.chart?.bars.length ?? 0)).toBe(true);
    // The chosen range is only offered while it narrows the span: otherwise the whole span is drawn.
    expect(model(views.short, '1d').range).toBe('all');
  });

  it('fits a short run: its bars are its own span, labelled at most six times, with no fixed week', () => {
    const short = model(views.short);
    const bars = short.chart?.bars ?? [];
    expect(bars.length).toBeGreaterThan(0);
    expect(bars.length).toBeLessThan(40);
    expect(short.chart?.marks.length ?? 0).toBeLessThanOrEqual(6);
    expect(short.ranges).toEqual([]);
  });

  it('names the actual CLI provider beside every model id, every legend name in the code face', () => {
    const week = model(views.week);
    const names = week.chart?.legend.map(item => item.label) ?? [];
    expect(names).toEqual(
      expect.arrayContaining(['Claude Code · claude-opus-5-5', 'Codex · gpt-5-codex']),
    );
    expect(names).toContain('Codex · default');
    expect(week.chart?.legend.every(item => item.mono === true)).toBe(true);
    const opus = week.models.find(line => line.name === 'Claude Code · claude-opus-5-5');
    expect(opus?.title).toBe('claude-opus-5-5 on Claude Code');
    expect(week.models.find(line => line.name === 'Codex · default')?.title).toBe(
      'Codex default model',
    );
  });

  it('puts every bucket at its own moment: bars grow in time, last as long as the host cut them, and the axis holds them all', () => {
    for (const name of COST_CASES) {
      for (const range of ['all', '1d'] as const) {
        const chart = model(views[name], range).chart;
        if (chart !== null) expectBarsAtTheirMoments(chart);
      }
    }
  });

  it("says in the chart's header how long each bar lasts", () => {
    expect(model(views.week).bucketWords).toMatch(/^per (\d+ )?(minute|hour|day)s?$/);
    expect(model(views.none).bucketWords).toBeNull();
  });

  it('draws a model with no priced turn muted and says so, and gives the others their color', () => {
    const short = model(views.short);
    const legend = short.chart?.legend ?? [];
    const unpriced = legend.filter(item => item.label.endsWith('(unpriced)'));
    expect(unpriced).toHaveLength(1);
    expect(unpriced[0]?.muted).toBe(true);
    expect(unpriced[0]?.series).toBeUndefined();
    expect(short.models.filter(line => line.series === null)).toHaveLength(1);
    for (const line of short.models.filter(each => each.series !== null)) {
      const item = legend.find(each => each.label === line.name);
      expect(item?.series).toBe(line.series);
    }
  });

  it('keeps spend from models outside the bounded split as an explicit part of each bar', () => {
    const view = views.short;
    const at = view.series.buckets.findIndex(bucket => bucket.costUsd > 0);
    const bucket = view.series.buckets[at];
    if (bucket === undefined) throw new Error('the short case has spend');
    const omitted = 2.5;
    const changed = {
      ...view,
      modelsTruncated: true,
      series: {
        ...view.series,
        buckets: view.series.buckets.map((each, index) =>
          index === at ? {...each, costUsd: each.costUsd + omitted} : each,
        ),
      },
    };
    const chart = model(changed).chart;
    const bar = chart?.bars.find(each => each.startMs === bucket.startMs);
    expect(bar?.segments.reduce((sum, segment) => sum + segment.value, 0)).toBe(bar?.total);
    expect(bar?.title).toContain('Other models $2.50');
    expect(chart?.legend.find(item => item.label === 'Other models')?.series).toBeDefined();
    expect(model(changed).modelsTruncated).toBe(true);
  });

  it('carries the mark of a floor on every figure that leaves out unpriced turns, the last hour too', () => {
    const short = totalFigures([views.short], 1);
    expect([short.floor, short.lastHourFloor]).toEqual([true, true]);
    const clean = totalFigures([{...views.short, unpricedTurns: 0}], 1);
    expect([clean.floor, clean.lastHourFloor]).toEqual([false, false]);
    // The mark and its figure are held together by a no-break space.
    const cell = model(views.short).runs[0]?.cost.text ?? '';
    expect(cell).toMatch(/^≥ \$/);
  });

  it('flags a run whose cost is past its budget, and says how far along it every other one is', () => {
    const week = model(views.week);
    const budgeted = week.runs.flatMap(line => (line.budget === null ? [] : [line.budget]));
    expect(budgeted.length).toBeGreaterThan(0);
    for (const budget of budgeted) {
      if (budget.over) expect(budget.hint).toMatch(/^Over budget by /);
      else expect(budget.hint).toMatch(/of the budget spent$|^No turn reported/);
    }
    expect(budgeted.some(budget => budget.over)).toBe(true);
    expect(budgeted.some(budget => !budget.over)).toBe(true);
  });

  it('dates a run where the client holds its card, and leaves a run it holds none of undated', () => {
    const run = views.week.runs[0];
    if (run === undefined) throw new Error('the week has runs');
    const started = hostCostModel('host-a', views.week, COST_OVERVIEW_WORDS, {
      range: 'all',
      sort: HOST_ORDER,
      started: {[run.runId]: Date.UTC(2026, 10, 3, 15)},
    });
    expect(started.runs[0]?.started?.text).toBe('Tue 3');
    expect(started.runs[1]?.started).toBeNull();
  });

  it("keeps a model's color when the range changes: it follows the model, not what is in view", () => {
    const colors = (range: SpendRange) =>
      new Map(model(views.week, range).chart?.legend.map(item => [item.label, item.series]));
    const all = colors('all');
    for (const [label, series] of colors('1d')) expect(all.get(label)).toBe(series);
  });

  it("gives each run its part of the host's total, and a run with no price none", () => {
    const week = model(views.week);
    const shares = week.runs.map(line => line.share);
    expect(shares[0]).toMatch(/^\d+%$/);
    expect(shares.at(-1)).toBe('—');
    expect(week.runs.map(line => line.key)).toEqual(
      views.week.runs.map(run => `host-a/${run.runId}`),
    );
  });
});

const hostView = (costUsd: number, unpricedTurns = 0): HostCostView => ({
  sequence: 1n,
  asOfMs: 1_000,
  total: usage({costUsd}),
  runCount: 1,
  turns: 2,
  unpricedTurns,
  runs: [],
  otherRuns: null,
  roles: [],
  models: [],
  series: {startMs: 0, bucketMs: 1_000, buckets: []},
  lastHourUsd: costUsd,
  modelsTruncated: false,
});

describe('the public operator cost figures', () => {
  it('divides complete priced spend by complete explored run-workstreams', () => {
    const figures = totalFigures(
      [hostView(40), hostView(60)],
      2,
      [],
      [
        {count: 2, complete: true},
        {count: 3, complete: true},
      ],
    );
    expect(figures.costPerWorkstream).toEqual({kind: 'average', figure: '$20.00'});
  });

  it('names every failed coverage proof instead of presenting a partial average', () => {
    const figures = totalFigures(
      [hostView(40, 1)],
      2,
      [],
      [
        {count: 0, complete: true},
        {count: 0, complete: false},
      ],
    );
    expect(figures.costPerWorkstream).toEqual({
      kind: 'unavailable',
      reason:
        '1 of 2 machine cost views available · 1 unpriced turn remains · 1 of 2 machines have complete workstream views · No explored workstreams',
    });
  });
});
