/**
 * The Ideas table over generated runs (`generated-runs.ts`): the best row states the multiple of the
 * chart's headline, the shares of the gain add up, the statuses partition the ideas, and the chips
 * and the sort say what they promise.
 */
import {describe, expect, test} from 'bun:test';
import type {Candidate} from '@vibesys/run-state/candidates';
import fc from 'fast-check';
import {run} from './generated-run.test-helper.js';
import {
  candidateOf,
  chartBest,
  type Run,
  readFromEvents,
  readFromWriter,
  viewedAt,
} from './generated-runs.js';
import {impactOf} from './idea-impact.js';
import type {IDEA_STATUSES} from './idea-status.js';
import {type IdeasSort, ideasTable} from './ideas-table-view.js';
import {IDEAS_FILTER_DEFAULT} from './ideas-view.js';
import {
  bestSoFar,
  type ObjectiveReading,
  objectiveSeriesOf,
  readObjectives,
} from './objective-reading.js';
import type {RunModel} from './run-model.js';
import {ratio} from './span-reading.js';

const SETTINGS = {numRuns: 300} as const;

const READERS = [
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
] as const;

function tableOf(
  model: RunModel,
  whole: RunModel,
  options: {sort?: IdeasSort | null; status?: (typeof IDEA_STATUSES)[number] | null} = {},
) {
  return ideasTable(model, {
    objectives: objectiveSeriesOf(whole),
    filter: {...IDEAS_FILTER_DEFAULT, status: options.status ?? null},
    sort: options.sort ?? null,
    selected: null,
    now: null,
  });
}

/** The best row of `viewed` at `scrub` states the multiple of the headline the chart of `whole` says. */
function expectBestRowToState(whole: RunModel, viewed: RunModel, scrub: number | null): void {
  const {headline} = chartBest(whole, scrub);
  const best = bestSoFar(readObjectives(whole)[0] as ObjectiveReading, scrub)?.idea;
  const row = tableOf(viewed, whole).rows.find(each => each.id === best);
  const multiple = row?.cells[0]?.multiple ?? null;
  if (headline === undefined || headline.ratio === null) {
    // No headline multiple: the best row, if it has one, is no better than the input.
    expect(multiple === null || headline === undefined).toBe(true);
    return;
  }
  expect(multiple === null ? null : ratio(multiple)).toBe(headline.ratio);
}

/** The readings of `values` up to the first missing one, which every later one is too. */
function expectReadingsFirst(values: readonly (number | null)[], direction: Run['direction']) {
  const none = values.indexOf(null);
  const read = values.slice(0, none < 0 ? values.length : none) as number[];
  expect(values.slice(none < 0 ? values.length : none).every(value => value === null)).toBe(true);
  for (let at = 1; at < read.length; at += 1) {
    const [before, after] = [read[at - 1] as number, read[at] as number];
    expect(direction === 'min' ? before <= after : before >= after).toBe(true);
  }
}

for (const [reader, read] of READERS) {
  describe(`the Ideas table of a run read from ${reader}`, () => {
    test('the best row states the multiple of the chart’s headline, at the end and at any position of the time bar', () => {
      fc.assert(
        fc.property(run, each => {
          const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
          const whole = read(each, candidates);
          const end = whole.latest ?? 0;
          for (const scrub of [null, Math.floor(each.cursor * end)]) {
            const viewed = scrub === null ? whole : viewedAt(whole, each, scrub, reader);
            expectBestRowToState(whole, viewed, scrub);
          }
        }),
        SETTINGS,
      );
    });

    test('the shares of the gain add up to the whole, and only the ideas that set a best have one', () => {
      fc.assert(
        fc.property(run, each => {
          const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
          const whole = read(each, candidates);
          const impact = impactOf(whole, objectiveSeriesOf(whole));
          if (impact.shares === null) return;
          const sum = [...impact.shares.values()].reduce((total, share) => total + share, 0);
          expect(sum + impact.unowned).toBeCloseTo(1, 9);
          for (const [id, share] of impact.shares) {
            expect(share).toBeGreaterThan(0);
            expect(impact.cells.get(id)?.[0]?.setBest).toBe(true);
          }
          // Each idea that set a best by a reading of its own owns its share.
          const owners = [...impact.cells]
            .filter(([, cells]) => cells[0]?.setBest === true)
            .map(([id]) => id);
          for (const id of impact.shares.keys()) expect(owners).toContain(id);
        }),
        SETTINGS,
      );
    });

    test('the statuses partition the ideas, and a chip lists exactly the ideas it counts', () => {
      fc.assert(
        fc.property(run, each => {
          const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
          const whole = read(each, candidates);
          const all = tableOf(whole, whole);
          const [every, ...chips] = all.chips;
          expect(every?.count).toBe(candidates.length);
          expect(all.rows).toHaveLength(candidates.length);
          expect(chips.reduce((total, chip) => total + chip.count, 0)).toBe(candidates.length);
          for (const chip of chips) {
            expect(chip.count).toBeGreaterThan(0);
            const listed = tableOf(whole, whole, {status: chip.status});
            expect(listed.rows).toHaveLength(chip.count);
            expect(new Set(listed.rows.map(row => row.status.status))).toEqual(
              new Set(chip.status === null ? [] : [chip.status]),
            );
          }
        }),
        SETTINGS,
      );
    });

    test('sorted by the objective, readings run best first in its direction and ideas with none come last', () => {
      fc.assert(
        fc.property(run, each => {
          const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
          const whole = read(each, candidates);
          const sorted = tableOf(whole, whole, {sort: {by: 'objective', index: 0}});
          expectReadingsFirst(
            sorted.rows.map(row => row.cells[0]?.value ?? null),
            each.direction,
          );
        }),
        SETTINGS,
      );
    });
  });
}
