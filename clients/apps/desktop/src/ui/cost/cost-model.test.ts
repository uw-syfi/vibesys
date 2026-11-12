import {describe, expect, expectTypeOf, it} from '@effect/vitest';
import {Harness} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {CostView} from '../../state/index.js';
import {costViewArb} from '../../testing/orchestrator/cost-arbitrary.js';
import {
  ENDED_RUN,
  LONG_NAMES,
  NO_PRICES,
  NO_TURNS,
  PRICED_AND_UNPRICED,
} from '../../testing/orchestrator/cost-views.js';
import {COST_WORDS} from '../test-display-clock.js';
import {
  type CostScreenModel,
  costScreenModel,
  KIND_COLUMNS,
  MODEL_COLUMNS,
  slotsOf,
  TYPE_COLUMNS,
} from './cost-model.js';
import {formatTokens} from './words.js';

const NUMBER_WORDS = /NaN|undefined|Infinity|\[object/;

/** Under half a cent, which the screen cannot write, is no spend left out of a bucket's models. */
const HALF_A_CENT = 0.005;

type Bucket = CostView['series']['buckets'][number];
const leftOut = (bucket: Bucket): number =>
  bucket.costUsd - bucket.byModel.reduce((sum, spend) => sum + spend.costUsd, 0);

/** Every string the model says, so a property can read all of it. */
function sayings(view: Parameters<typeof costScreenModel>[0]): string[] {
  const model = costScreenModel(view, COST_WORDS);
  return [
    model.spend.figure ?? '',
    model.spend.cap ?? '',
    model.spend.lastHour ?? '',
    model.spend.turns,
    model.spend.unpricedTurns,
    ...[...model.types, ...model.models, ...model.kinds].flatMap(row => [
      row.name,
      row.note ?? '',
      ...row.figures.map(figure => figure ?? ''),
    ]),
    ...(model.chart?.bars.map(bar => bar.title) ?? []),
    ...(model.chart?.marks.map(mark => mark.text) ?? []),
  ];
}

describe('the cost screen model', () => {
  it('builds every row with one figure for each header of its table', () => {
    // The types are the proof: a row of another width is not a `BarRow` of this table's width.
    expectTypeOf<CostScreenModel['types'][number]['figures']['length']>().toEqualTypeOf<
      (typeof TYPE_COLUMNS)['length']
    >();
    expectTypeOf<CostScreenModel['models'][number]['figures']['length']>().toEqualTypeOf<
      (typeof MODEL_COLUMNS)['length']
    >();
    expectTypeOf<CostScreenModel['kinds'][number]['figures']['length']>().toEqualTypeOf<
      (typeof KIND_COLUMNS)['length']
    >();
    fc.assert(
      fc.property(costViewArb, view => {
        const model = costScreenModel(view, COST_WORDS);
        for (const row of model.types) expect(row.figures).toHaveLength(TYPE_COLUMNS.length);
        for (const row of model.models) expect(row.figures).toHaveLength(MODEL_COLUMNS.length);
        for (const row of model.kinds) expect(row.figures).toHaveLength(KIND_COLUMNS.length);
      }),
    );
  });

  it('says only what was served: a price that was never reported stays absent and is never a zero', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        const model = costScreenModel(view, COST_WORDS);
        expect(model.spend.figure === null).toBe(view.total.costUsd === null);
        expect(model.spend.lastHour === null).toBe(view.lastHourUsd === null);
        expect(model.spend.cap === null).toBe(view.maxCostUsd === null);
        expect(model.spend.capShare === null).toBe(
          view.total.costUsd === null || view.maxCostUsd === null,
        );
        for (const [rows, served] of [
          [model.types, view.roles],
          [model.models, view.models],
        ] as const) {
          rows.forEach((row, at) => {
            const cost = served[at]?.usage.costUsd ?? null;
            expect(row.value).toBe(cost);
            expect(row.figures.at(-1) === null).toBe(cost === null);
            expect(row.note === 'No price').toBe(cost === null);
          });
        }
        // The Tokens column is the number the host served for the type or model, as the host
        // served it: the screen adds up no kinds, so it shows a total that is not their sum.
        expect(model.types.map(row => row.figures.at(-2))).toEqual(
          view.roles.map(role => formatTokens(role.totalTokens)),
        );
        expect(model.models.map(row => row.figures.at(-2))).toEqual(
          view.models.map(cost => formatTokens(cost.totalTokens)),
        );
        expect(model.kinds.map(row => row.figures[0] === null)).toEqual([
          view.total.inputTokens === null,
          view.total.cacheReadTokens === null,
          view.total.cacheCreationTokens === null,
          view.total.outputTokens === null,
          view.total.reasoningTokens === null,
        ]);
      }),
    );
  });

  it('keeps the order the host sent and the value it sent, never sorting or adding up', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        const model = costScreenModel(view, COST_WORDS);
        expect(model.types.map(row => row.value)).toEqual(view.roles.map(r => r.usage.costUsd));
        expect(model.models.map(row => row.value)).toEqual(view.models.map(m => m.usage.costUsd));
        if (model.chart !== null) {
          const hasOther = (bucket: Bucket): boolean =>
            view.modelsTruncated && leftOut(bucket) >= HALF_A_CENT;
          expect(model.chart.bars.map(bar => bar.total)).toEqual(
            view.series.buckets.map(bucket => bucket.costUsd),
          );
          expect(model.chart.bars.map(bar => bar.segments.length)).toEqual(
            view.series.buckets.map(bucket => bucket.byModel.length + (hasOther(bucket) ? 1 : 0)),
          );
          expect(model.chart.legend.length).toBe(
            view.models.length + (view.series.buckets.some(hasOther) ? 1 : 0),
          );
        }
      }),
    );
  });

  it('is a floor exactly when a price is shown and turns without one are behind it', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        expect(costScreenModel(view, COST_WORDS).spend.floor).toBe(
          view.total.costUsd !== null && view.unpricedTurns > 0,
        );
      }),
    );
  });

  it('gives a model the same color however the host orders the models, as the run goes on', () => {
    fc.assert(
      fc.property(costViewArb, fc.nat(), (view, shift) => {
        const turned = view.models.length === 0 ? 0 : shift % view.models.length;
        const reordered = {
          ...view,
          models: [...view.models.slice(turned), ...view.models.slice(0, turned)],
        };
        const colors = (v: typeof view) =>
          new Map(costScreenModel(v, COST_WORDS).models.map(row => [row.key, row.series]));
        expect(colors(reordered)).toEqual(colors(view));
      }),
    );
  });

  it('labels the first bucket and then round times, in order, and draws no label a bar has not got', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        const chart = costScreenModel(view, COST_WORDS).chart;
        const marks = chart?.marks ?? [];
        if (chart !== null) {
          expect(marks[0]?.at).toBe(0);
          expect(marks[0]?.text).toBe('0');
        }
        expect(marks.map(mark => mark.at)).toEqual(
          [...new Set(marks.map(mark => mark.at))].sort((a, b) => a - b),
        );
        for (const mark of marks) expect(mark.at).toBeLessThan(view.series.buckets.length);
      }),
    );
  });

  it('writes nothing that is not a word or a number, for any view', () => {
    fc.assert(
      fc.property(costViewArb, view => {
        for (const text of sayings(view)) expect(text).not.toMatch(NUMBER_WORDS);
      }),
    );
  });

  it('writes every figure of a screen at one precision', () => {
    const cents = (text: string | null) => text !== null && /\.\d\d(\/h)?$/.test(text);
    const dollars = (text: string | null) => text !== null && !text.includes('.');
    const model = costScreenModel(PRICED_AND_UNPRICED, COST_WORDS);
    // The run costs $318: whole dollars everywhere a dollar figure is written.
    expect(model.spend.figure).toBe('$318');
    expect(model.spend.cap).toBe('$500');
    expect(model.spend.lastHour).toBe('$14/h');
    for (const row of [...model.types, ...model.models]) {
      const figure = row.figures.at(-1) ?? null;
      expect(figure === null || dollars(figure)).toBe(true);
    }
    const cheap = costScreenModel(
      {...PRICED_AND_UNPRICED, total: {...PRICED_AND_UNPRICED.total, costUsd: 12.5}},
      COST_WORDS,
    );
    expect(cents(cheap.spend.figure)).toBe(true);
    expect(cents(cheap.spend.lastHour)).toBe(true);
  });

  it('draws a chart for named or explicitly truncated model spend, and none without either', () => {
    expect(costScreenModel(NO_PRICES, COST_WORDS).chart).toBeNull();
    expect(costScreenModel(PRICED_AND_UNPRICED, COST_WORDS).chart).not.toBeNull();
    fc.assert(
      fc.property(costViewArb, view => {
        const priced = view.series.buckets.some(
          bucket =>
            bucket.byModel.length > 0 || (view.modelsTruncated && leftOut(bucket) >= HALF_A_CENT),
        );
        expect(costScreenModel(view, COST_WORDS).chart === null).toBe(!priced);
      }),
    );
  });

  it('is empty exactly when no turn has finished', () => {
    expect(costScreenModel(NO_TURNS, COST_WORDS).empty).toBe(true);
    expect(costScreenModel(NO_PRICES, COST_WORDS).empty).toBe(false);
    fc.assert(
      fc.property(costViewArb, view => {
        expect(costScreenModel(view, COST_WORDS).empty).toBe(view.turns === 0);
      }),
    );
  });

  it('names an unpriced type, an unpriced model and a run with a price missing for some turns', () => {
    const model = costScreenModel(PRICED_AND_UNPRICED, COST_WORDS);
    expect(model.types.map(row => [row.name, row.note])).toEqual([
      ['Worker', undefined],
      ['Lead', '12 unpriced'],
      ['Reviewer', undefined],
      ['Profiler', 'No price'],
    ]);
    expect(model.types.at(-1)?.figures).toEqual(['×1', '41', '2.2M', null]);
    expect(model.spend.floor).toBe(true);
    expect(costScreenModel(ENDED_RUN, COST_WORDS).spend.floor).toBe(false);
    expect(costScreenModel(NO_PRICES, COST_WORDS).spend.figure).toBeNull();
  });

  it('makes "Other models" of spend the split left out and never of floating-point noise from summing it', () => {
    const base = PRICED_AND_UNPRICED;
    const at = base.series.buckets.findIndex(bucket => bucket.byModel.length >= 2);
    const bucket = base.series.buckets[at];
    if (bucket === undefined) throw new Error('the priced case has a bucket with two models');
    /** The view whose bucket `at` holds `byModel` and whose total is `total`, the models cut short. */
    const viewOf = (costs: readonly number[], total: number): CostView =>
      ({
        ...base,
        modelsTruncated: true,
        series: {
          ...base.series,
          buckets: base.series.buckets.map((each, index) =>
            index === at
              ? {
                  ...each,
                  costUsd: total,
                  byModel: costs.map((costUsd, i) => ({
                    model: each.byModel[i % each.byModel.length]?.model ?? each.byModel[0]?.model,
                    costUsd,
                  })),
                }
              : {...each, costUsd: 0, byModel: []},
          ),
        },
      }) as CostView;
    const hasOther = (view: CostView): boolean =>
      (costScreenModel(view, COST_WORDS).chart?.legend ?? []).some(
        item => item.label === 'Other models',
      );
    fc.assert(
      fc.property(
        fc.array(fc.double({min: 0.001, max: 50, noNaN: true}), {minLength: 1, maxLength: 8}),
        costs => {
          // The total is the same models' sum taken in the other order: equal but for rounding.
          const total = [...costs].reverse().reduce((sum, cost) => sum + cost, 0);
          expect(hasOther(viewOf(costs, total))).toBe(false);
        },
      ),
    );
    fc.assert(
      fc.property(
        fc.array(fc.double({min: 0.001, max: 50, noNaN: true}), {minLength: 1, maxLength: 8}),
        fc.double({min: 0.01, max: 50, noNaN: true}),
        (costs, omitted) => {
          const total = costs.reduce((sum, cost) => sum + cost, 0) + omitted;
          const chart = costScreenModel(viewOf(costs, total), COST_WORDS).chart;
          expect(hasOther(viewOf(costs, total))).toBe(true);
          const bar = chart?.bars.find(each => each.startMs === bucket.startMs);
          expect(bar?.title).toContain('Other models');
        },
      ),
    );
  });

  it('names the actual CLI provider beside every model id the host served', () => {
    expect(costScreenModel(PRICED_AND_UNPRICED, COST_WORDS).models.map(row => row.name)).toEqual([
      'Claude Code · model-a',
      'Codex · model-b',
      'Codex · default',
    ]);
    const long = costScreenModel(LONG_NAMES, COST_WORDS);
    expect(long.models[0]?.name.length).toBeGreaterThan(60);
    expect(long.models[0]?.mono).toBe(true);
    expect(long.chart?.legend[0]?.mono).toBe(true);
    // The harness's default is no id, but it stands in the same column and legend, so it reads alike.
    const own = costScreenModel(PRICED_AND_UNPRICED, COST_WORDS).models.at(-1);
    expect(own?.name).toBe('Codex · default');
    expect(own?.mono).toBe(true);
    const all = costScreenModel(PRICED_AND_UNPRICED, COST_WORDS);
    expect(all.models.every(row => row.mono === true)).toBe(true);
    expect(all.chart?.legend.every(item => item.mono === true)).toBe(true);
  });

  it('gives a model a color that is its own: the same in every view, whoever else is on the page', () => {
    const colors = (ids: readonly string[]) => {
      const view = {
        ...PRICED_AND_UNPRICED,
        models: ids.map(model => ({
          ...(PRICED_AND_UNPRICED.models[0] as (typeof PRICED_AND_UNPRICED.models)[number]),
          model: {harness: Harness.CODEX, model},
        })),
        series: {...PRICED_AND_UNPRICED.series, buckets: []},
      };
      return slotsOf(view);
    };
    const key = (model: string) => `${Harness.CODEX}/${model}`;
    // Alone or with others, a model that no other holds the slot of keeps it.
    const alone = colors(['claude-opus-5-5']).get(key('claude-opus-5-5'));
    for (const others of [['gpt-5-codex'], ['model-a', 'model-b'], ['claude-haiku-5-5', 'zzz']]) {
      const together = colors(['claude-opus-5-5', ...others]);
      const slots = [...together.values()];
      // No two models of a view draw alike, and all are in the palette.
      expect(new Set(slots).size).toBe(slots.length);
      for (const slot of slots) expect(slot).toBeLessThan(8);
      if (together.get(key('claude-opus-5-5')) !== alone) {
        // Only a clash of the hash moves a model, and then to a slot nobody else holds.
        expect(others.some(other => together.get(key(other)) === alone)).toBe(true);
      }
    }
    // The same models in any order are the same colors.
    expect(colors(['a1', 'b2', 'c3'])).toEqual(colors(['c3', 'a1', 'b2']));
  });
});
