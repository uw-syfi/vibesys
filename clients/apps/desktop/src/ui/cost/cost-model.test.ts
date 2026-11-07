import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {costViewArb} from '../../testing/orchestrator/cost-arbitrary.js';
import {
  ENDED_RUN,
  LONG_NAMES,
  NO_PRICES,
  NO_TURNS,
  PRICED_AND_UNPRICED,
} from '../../testing/orchestrator/cost-views.js';
import {COST_WORDS} from '../orchestrator/cost-words.js';
import {costScreenModel} from './cost-model.js';

const NUMBER_WORDS = /NaN|undefined|Infinity|\[object/;

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
          expect(model.chart.bars.map(bar => bar.total)).toEqual(
            view.series.buckets.map(bucket => bucket.costUsd),
          );
          expect(model.chart.bars.map(bar => bar.segments.length)).toEqual(
            view.series.buckets.map(bucket => bucket.byModel.length),
          );
          expect(model.chart.legend.length).toBe(view.models.length);
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

  it('draws no chart where no bucket holds a priced model, and a chart where one does', () => {
    expect(costScreenModel(NO_PRICES, COST_WORDS).chart).toBeNull();
    expect(costScreenModel(PRICED_AND_UNPRICED, COST_WORDS).chart).not.toBeNull();
    fc.assert(
      fc.property(costViewArb, view => {
        const priced = view.series.buckets.some(bucket => bucket.byModel.length > 0);
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
    expect(model.types.at(-1)?.figures).toEqual(['×1', '41', null]);
    expect(model.spend.floor).toBe(true);
    expect(costScreenModel(ENDED_RUN, COST_WORDS).spend.floor).toBe(false);
    expect(costScreenModel(NO_PRICES, COST_WORDS).spend.figure).toBeNull();
  });

  it('shows the model id as the host served it, and the harness for its own default', () => {
    expect(costScreenModel(PRICED_AND_UNPRICED, COST_WORDS).models.map(row => row.name)).toEqual([
      'model-a',
      'model-b',
      'Codex',
    ]);
    const long = costScreenModel(LONG_NAMES, COST_WORDS);
    expect(long.models[0]?.name.length).toBeGreaterThan(60);
    expect(long.models[0]?.mono).toBe(true);
    expect(long.chart?.legend[0]?.mono).toBe(true);
  });
});
