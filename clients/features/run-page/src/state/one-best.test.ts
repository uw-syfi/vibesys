/**
 * One definition of "best". The headline multiple of a chart, the end of its best-so-far line and the
 * `vs input` of the best row of the Ideas list are read from one selector, so over any generated
 * run, at the run's end and at any position of the time bar, they state one multiple of one input
 * (the generated runs are in `generated-runs.ts`).
 */
import {describe, expect, test} from 'bun:test';
import type {Candidate} from '@vibesys/run-state/candidates';
import fc from 'fast-check';
import {
  bestRow,
  candidateOf,
  chartBest,
  type Run,
  readFromEvents,
  readFromWriter,
  rowOf,
  run,
  viewedAt,
} from './generated-runs.js';
import {IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {
  bestSoFar,
  multipleOver,
  type ObjectiveReading,
  readObjectives,
} from './objective-reading.js';
import {ratio} from './span-reading.js';

const SETTINGS = {numRuns: 300} as const;

for (const [reader, read] of [
  ['events', (each: Run, candidates: readonly Candidate[]) => readFromEvents(each, candidates)],
  [
    'the writer',
    (each: Run, candidates: readonly Candidate[]) =>
      readFromWriter(
        each,
        candidates,
        Math.max(0, ...candidates.map(item => item.measurement?.at ?? 0)),
      ),
  ],
] as const) {
  describe(`the best of an objective, as read from ${reader}`, () => {
    test('the headline, the end of the line and the best row state one multiple of the one input', () => {
      fc.assert(
        fc.property(run, each => {
          const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
          const whole = read(each, candidates);
          const {chart, headline, reached} = chartBest(whole, null);
          const row = bestRow(whole);
          // A run with no best has no best row, and no headline multiple, and a flat line.
          if (headline?.ratio === null || headline === undefined) {
            expect(row?.figure.includes('×') ?? false).toBe(false);
            return;
          }
          const input = chart?.steps[0]?.y;
          expect(input).toBeDefined();
          const multiple = multipleOver(reached?.y as number, input as number, each.direction);
          expect(headline.ratio).toBe(ratio(multiple as number));
          expect(row?.figure).toBe(headline.ratio);
        }),
        SETTINGS,
      );
    });

    test('at any position of the time bar they agree on the best by then', () => {
      fc.assert(
        fc.property(run, each => {
          const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
          const whole = read(each, candidates);
          const end = whole.latest ?? 0;
          const scrub = Math.floor(each.cursor * end);
          const {chart, headline, reached} = chartBest(whole, scrub);
          // The best row is the row of the idea that set the line's last step by then.
          const setter = bestSoFar(readObjectives(whole)[0] as ObjectiveReading, scrub)?.idea;
          const row = rowOf(viewedAt(whole, each, scrub, reader), setter ?? null);
          if (headline?.ratio === null || headline === undefined) {
            expect(row?.figure.includes('×') ?? false).toBe(false);
            return;
          }
          const multiple = multipleOver(
            reached?.y as number,
            chart?.steps[0]?.y as number,
            each.direction,
          );
          expect(headline.ratio).toBe(ratio(multiple as number));
          expect(row?.figure).toBe(headline.ratio);
        }),
        SETTINGS,
      );
    });
  });
}

describe('the input is one value', () => {
  test('every idea’s `vs input` is over the input the chart starts from, not that of its session', () => {
    fc.assert(
      fc.property(run, each => {
        const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
        const whole = readFromEvents(each, candidates);
        const {chart} = chartBest(whole, null);
        const input = chart?.steps[0]?.y;
        for (const row of ideasView(whole, IDEAS_FILTER_DEFAULT).rows) {
          const item = candidates.find(candidate => candidate.id === row.id);
          if (item === undefined || item.outcome === 'running' || input === undefined) continue;
          const multiple = multipleOver(item.measurement?.metricValue ?? 0, input, each.direction);
          expect(row.figure).toBe(multiple === null ? '–' : ratio(multiple));
        }
      }),
      SETTINGS,
    );
  });

  test('a run the writer states has the input it states, whatever session an idea was measured in', () => {
    fc.assert(
      fc.property(run, each => {
        const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
        const whole = readFromWriter(each, candidates, 700);
        expect(chartBest(whole, null).chart?.steps[0]?.y).toBe(each.input);
      }),
      SETTINGS,
    );
  });
});
