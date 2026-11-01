/**
 * The Ideas table over generated runs: the best row states the multiple of the chart's headline
 * (checked against a plain `value / input`), the shares of the gain are each idea's by definition,
 * the statuses partition the ideas, and the chips and the sort say what they promise, at the end of
 * the run and at any position of the time bar. The generated runs of one to six objectives
 * (`shaped-runs.test-helper.ts`) are read the way the app reads them at a position: the run to its
 * end as its writer states it, and the run at the position without the writer's decisions.
 */
import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {fromBinary} from '@bufbuild/protobuf';
import {KeyframeSchema, RunHeadSchema} from '@vibesys/proto';
import type {Skeleton} from '@vibesys/run-skeleton';
import {seedSkeleton} from '@vibesys/run-skeleton';
import type {Candidate} from '@vibesys/run-state/candidates';
import fc from 'fast-check';
import {
  candidateOf,
  chartBest,
  READERS,
  type Run,
  generatedRuns as run,
  viewedAt,
} from './generated-runs.test-helper.js';
import {heroView} from './headline.js';
import {impactOf, type ObjectiveCell} from './idea-impact.js';
import type {IDEA_STATUSES} from './idea-status.js';
import {type IdeasSort, type IdeaTableRow, ideasTable} from './ideas-table-view.js';
import {modelOfShape} from './ideas-tree-fixtures.js';
import {IDEAS_FILTER_DEFAULT} from './ideas-view.js';
import {objectiveCharts} from './objective-chart.js';
import {
  bestSoFar,
  bestSteps,
  type ObjectiveReading,
  objectiveSeriesOf,
  readObjectives,
  stepsUntil,
} from './objective-reading.js';
import {clockStart} from './run-level.js';
import type {RunModel} from './run-model.js';
import {
  type Expected,
  type ExpectedCell,
  endOfRun,
  expectedAt,
  inputOf,
  momentsOf,
  moved,
  type ObjectiveSpec,
  type ShapedRun,
  scrubOf,
  shapedRuns,
  shapedRunsOf,
  startOf,
  viewedAt as viewedLikeTheApp,
  wholeOf,
} from './shaped-runs.test-helper.js';
import {projectSkeleton} from './skeleton-projection.js';
import {ratio} from './span-reading.js';
import {positionOf} from './time-position.js';

const SETTINGS = {numRuns: 300} as const;

/** The table of `model`, the run at the position `scrub` (run-clock seconds; null: the end) of `whole`. */
function tableOf(
  model: RunModel,
  whole: RunModel,
  scrub: number | null,
  options: {sort?: IdeasSort | null; status?: (typeof IDEA_STATUSES)[number] | null} = {},
) {
  return ideasTable(model, {
    position: {whole, scrub},
    filter: {...IDEAS_FILTER_DEFAULT, status: options.status ?? null},
    sort: options.sort ?? null,
    selected: null,
    now: null,
  });
}

/** The best row of `viewed` at `scrub` states the multiple of the headline the chart of `whole` says. */
function expectBestRowToState(
  whole: RunModel,
  viewed: RunModel,
  scrub: number | null,
  direction: Run['direction'],
  /** The run's writer states its best as it likes: a best no idea set has no row. */
  ownerless: boolean,
): void {
  const {headline, input} = chartBest(whole, scrub);
  const best = bestSoFar(readObjectives(whole)[0] as ObjectiveReading, scrub)?.idea;
  if (ownerless && (best === undefined || best === null)) return;
  const row = tableOf(viewed, whole, scrub).rows.find(each => each.id === best);
  const cell = row?.cells[0];
  const multiple = cell?.multiple ?? null;
  if (multiple !== null && cell?.value != null && input !== undefined) {
    // The multiple is the plain ratio of the row's value to the input where the direction says
    // which way is better, whatever `multipleOver` does: both sides of the next check read it.
    const plain = direction === 'max' ? cell.value / input : input / cell.value;
    expect(multiple).toBeCloseTo(plain, 9);
  }
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
            expectBestRowToState(whole, viewed, scrub, each.direction, reader === 'the writer');
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
          // A writer may state a best a little worse than its line's: the last step then loses, and
          // no other idea can.
          const last = bestSteps(readObjectives(whole)[0] as ObjectiveReading).at(-1);
          for (const [id, share] of impact.shares) {
            if (share <= 0) expect(id).toBe(last?.idea ?? '');
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
          const all = tableOf(whole, whole, null);
          const [every, ...chips] = all.chips;
          expect(every?.count).toBe(candidates.length);
          expect(all.rows).toHaveLength(candidates.length);
          expect(chips.reduce((total, chip) => total + chip.count, 0)).toBe(candidates.length);
          for (const chip of chips) {
            expect(chip.count).toBeGreaterThan(0);
            const listed = tableOf(whole, whole, null, {status: chip.status});
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
          const sorted = tableOf(whole, whole, null, {sort: {by: 'objective', index: 0}});
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

describe('an idea compared with its base', () => {
  const SHAPE = {
    parents: [null, 0, 1, 0, 3, 3, 5],
    outcomes: ['passed', 'passed', 'passed', 'passed', 'failed', 'passed', 'passed'],
  } as const;

  /** Idea 3 states a ratio to its base, none to the input; the run's input is 100 (ideas 0, 1, 2 state theirs). */
  function compared(): RunModel {
    const model = modelOfShape(SHAPE);
    const candidates = model.candidates.candidates.map(item =>
      item.id === 'n3' && item.measurement !== null
        ? {
            ...item,
            measurement: {
              ...item.measurement,
              vsBaseline: null,
              against: {
                ratio: 1.1,
                baseCommit: 'x',
                baseValue: 100,
                verdict: 'better' as const,
              },
            },
          }
        : item,
    );
    return {...model, candidates: {...model.candidates, candidates}};
  }

  test('states its multiple of the run’s input when the run has one, not the ratio to its base', () => {
    const model = compared();
    const row = tableOf(model, model, null).rows.find(each => each.id === 'n3');
    // Idea 3 measured 103 against an input of 100: what the chart's tip says of it.
    expect(row?.cells[0]?.multiple).toBeCloseTo(1.03, 9);
  });
});

/** The run-clock positions to read `run` at: the end (null), the cursor, and around an idea's reading. */
function positionsOf(each: ShapedRun): readonly (number | null)[] {
  return [null, scrubOf(each), ...momentsOf(each)];
}

interface Reading {
  readonly whole: RunModel;
  /** The run at the position as the skeleton projection gives it: no decision on any reading. */
  readonly viewed: RunModel;
  readonly table: ReturnType<typeof tableOf>;
  readonly scrub: number | null;
}

/** Calls `check` with the table of `each` at the end, at its cursor and around one idea's reading. */
function atEveryPosition(each: ShapedRun, check: (reading: Reading) => void): void {
  const whole = wholeOf(each);
  for (const scrub of positionsOf(each)) {
    const viewed = scrub === null ? whole : viewedLikeTheApp(each, whole, scrub);
    check({whole, viewed, table: tableOf(viewed, whole, scrub), scrub});
  }
}

const directionOf = (each: ShapedRun, k: number) => (each.objectives[k] as ObjectiveSpec).direction;

function expectClose(got: number | null | undefined, want: number | null, digits: number): void {
  if (want === null) expect(got ?? null).toBeNull();
  else expect(got).toBeCloseTo(want, digits);
}

/** `cell` says what `want` defines; no `want` is an idea with no reading yet. */
function expectCell(cell: ObjectiveCell | undefined, want: ExpectedCell | undefined, id: string) {
  if (want === undefined) {
    expect(cell?.value ?? null, `${id} has no reading yet`).toBeNull();
    expect(cell?.setBest ?? false).toBe(false);
    return;
  }
  expect(cell?.value).toBe(want.value);
  expect(cell?.setBest, `${id} sets a best`).toBe(want.setBest);
  expectClose(cell?.multiple, want.multiple, 9);
  expectClose(cell?.gain, want.gain, 6);
}

function expectCells(each: ShapedRun, {table, scrub}: Reading): void {
  const started = each.ideas.filter((_, at) => scrub === null || startOf(at) <= scrub);
  expect(table.rows).toHaveLength(started.length);
  for (const [at, column] of table.columns.entries()) {
    const expected = expectedAt(each, column.index, scrub);
    for (const row of table.rows) expectCell(row.cells[at], expected.cells.get(row.id), row.id);
  }
}

const FORMS = ['value', 'value', 'value-multiple', 'multiple'] as const;

function expectColumns(each: ShapedRun, {table}: Reading): void {
  const movers = each.objectives.map((_, k) => k).filter(k => moved(each, k));
  const shown = movers.length > 0 ? movers : [0];
  // The same columns at every position: scrubbing adds and drops none.
  expect(table.columns.map(column => column.index)).toEqual(shown.slice(0, 4));
  expect(table.columns.length + table.more).toBe(shown.length);
  expect(table.form).toBe(FORMS[Math.min(table.columns.length, 3)] ?? 'value');
}

/** The best row of each objective states the multiple of the chart's headline at the position. */
function expectBestRows(each: ShapedRun, {whole, table, scrub}: Reading): void {
  const headlines = heroView(whole, undefined, scrub).headlines;
  for (const [at, column] of table.columns.entries()) {
    const best = expectedAt(each, column.index, scrub).best;
    const multiple = table.rows.find(row => row.id === best?.idea)?.cells[at]?.multiple;
    const stated = headlines[column.index]?.ratio;
    if (multiple != null && stated != null) expect(ratio(multiple)).toBe(stated);
  }
}

type Dots = ReturnType<typeof objectiveCharts>[number]['readings'];

/** The value of an idea read once is a dot of the chart. */
function expectDot(each: ShapedRun, dots: Dots, row: IdeaTableRow, at: number): void {
  const value = row.cells[at]?.value;
  const once = each.ideas[Number(row.id.slice(1))]?.again === null;
  if (value == null || !once) return;
  expect(dots.filter(dot => dot.id === row.id).map(dot => dot.y)).toContain(value);
}

/**
 * Every value of the table is a dot of the chart: one source at every position. (The chart draws
 * every reading of the run, the dimmed ones after the cursor too, and the last of an idea read
 * twice; the table shows the idea as of the position, so an idea read twice is left out.)
 */
function expectDots(each: ShapedRun, {whole, table, scrub}: Reading): void {
  const charts = objectiveCharts(whole, {start: 0, now: whole.latest, scrub});
  for (const [at, column] of table.columns.entries()) {
    const dots = charts[column.index]?.readings ?? [];
    for (const row of table.rows) expectDot(each, dots, row, at);
  }
}

function expectShare(share: IdeaTableRow['share'], expected: Expected, id: string): void {
  const want = expected.shares?.get(id);
  if (want === undefined) {
    expect(share, `${id} owns no step`).toBeNull();
    return;
  }
  expect(share?.percent).toBe(Math.round(want * 100));
  expect(share?.gain).toBeCloseTo(expected.cells.get(id)?.gain ?? Number.NaN, 6);
}

function expectShares(each: ShapedRun, {table, scrub}: Reading): void {
  const expected = expectedAt(each, 0, scrub);
  expect(table.shares).toBe(expected.shares !== null);
  for (const row of table.rows) expectShare(row.share, expected, row.id);
  const sum = [...(expected.shares?.values() ?? [1])].reduce((total, share) => total + share, 0);
  expect(sum).toBeCloseTo(1, 9);
}

function expectSortedByEveryColumn(each: ShapedRun, {whole, viewed, table, scrub}: Reading): void {
  for (const [at, column] of table.columns.entries()) {
    const sort = {by: 'objective', index: column.index} as const;
    const sorted = tableOf(viewed, whole, scrub, {sort});
    const values = sorted.rows.map(row => row.cells[at]?.value ?? null);
    expectReadingsFirst(values, directionOf(each, column.index));
  }
}

/**
 * The rows in the order of the default sort by definition: the ideas still running before them all
 * (at the live end and in a replay alike), then the first column's readings best first in its
 * direction, ideas with none last, ties newest first.
 */
function defaultOrder(
  rows: readonly IdeaTableRow[],
  direction: ObjectiveSpec['direction'],
): string[] {
  const value = (row: IdeaTableRow): number | null => row.cells[0]?.value ?? null;
  const pinned = (row: IdeaTableRow): number => (row.status.status === 'running' ? 0 : 1);
  const better = (a: number | null, b: number | null): number => {
    if (a === null || b === null) return Number(a === null) - Number(b === null);
    return direction === 'max' ? b - a : a - b;
  };
  const newest = (a: IdeaTableRow, b: IdeaTableRow): number =>
    Number(b.id.slice(1)) - Number(a.id.slice(1));
  return [...rows]
    .sort((a, b) => pinned(a) - pinned(b) || better(value(a), value(b)) || newest(a, b))
    .map(row => row.id);
}

/** The rows are in the default order of the first column, the ideas still running before them all. */
function expectDefaultOrder(each: ShapedRun, {table}: Reading): void {
  const first = table.columns[0];
  if (first === undefined) return;
  expect(table.defaultSort).toEqual({by: 'objective', index: first.index});
  const direction = directionOf(each, first.index);
  expect(table.rows.map(row => row.id)).toEqual(defaultOrder(table.rows, direction));
  // Every running idea leads, and only a running idea says what it is doing.
  const running = table.rows.map(row => row.status.status === 'running');
  expect(running).toEqual([...running].sort((a, b) => Number(b) - Number(a)));
  for (const row of table.rows) expect(row.doing !== null).toBe(row.status.status === 'running');
}

describe('the Ideas table of a run of one to six objectives, read as the app reads it at a position', () => {
  test('every dot, value, multiple and gain is by definition, at the end and at any position', () => {
    fc.assert(
      fc.property(shapedRuns, each => atEveryPosition(each, at => expectCells(each, at))),
      SETTINGS,
    );
  });

  test('the columns are the objectives that moved, at most four, and the rest are counted', () => {
    fc.assert(
      fc.property(shapedRuns, each => atEveryPosition(each, at => expectColumns(each, at))),
      SETTINGS,
    );
  });

  test('the best row of every objective states the multiple of the chart’s headline at that position', () => {
    fc.assert(
      fc.property(shapedRuns, each =>
        atEveryPosition(each, at => {
          expectBestRows(each, at);
          expectDots(each, at);
        }),
      ),
      SETTINGS,
    );
  });

  test('the share of the gain is each idea’s part of the line, and there is none without the input', () => {
    fc.assert(
      fc.property(shapedRunsOf(1), each => atEveryPosition(each, at => expectShares(each, at))),
      SETTINGS,
    );
  });

  test('sorted by any column, readings run best first in that objective’s direction and ideas with none last', () => {
    fc.assert(
      fc.property(shapedRuns, each =>
        atEveryPosition(each, at => expectSortedByEveryColumn(each, at)),
      ),
      SETTINGS,
    );
  });

  test('by default the first column runs best first, the ideas still running before them, at every position', () => {
    fc.assert(
      fc.property(shapedRuns, each => atEveryPosition(each, at => expectDefaultOrder(each, at))),
      SETTINGS,
    );
  });

  test('the ideas still running come first at the live end and in a replay of the same run', () => {
    fc.assert(
      fc.property(shapedRuns, each => {
        if (each.ideas.length < 2) return;
        const live = lastIdeaRunning(wholeOf(each));
        for (const scrub of [null, scrubOf(each)]) {
          const {rows, columns} = tableOf(live, live, scrub);
          const first = columns[0];
          if (first === undefined) continue;
          expect(rows.map(row => row.id)).toEqual(
            defaultOrder(rows, directionOf(each, first.index)),
          );
          if (scrub === null) expect(rows[0]?.status.status).toBe('running');
        }
      }),
      SETTINGS,
    );
  });

  test('a sort the reader chose is theirs: a running idea sorts with the rest', () => {
    fc.assert(
      fc.property(shapedRuns, each => {
        if (each.ideas.length < 2) return;
        const live = lastIdeaRunning(wholeOf(each));
        const sorted = tableOf(live, live, null, {sort: {by: 'agent-time'}});
        const seconds = sorted.rows.map(row => row.agentSeconds);
        expect(seconds).toEqual([...seconds].sort((a, b) => b - a));
      }),
      SETTINGS,
    );
  });

  test('a run whose first objective never moved sorts by the first column it shows', () => {
    const calm: ShapedRun = {
      objectives: [
        {name: 'throughput', direction: 'max', input: 50000},
        {name: 'p99_latency', direction: 'min', input: 20000},
      ],
      inputKnown: true,
      ideas: [
        {passed: true, ratios: [1, 0.5], again: null},
        {passed: true, ratios: [1.0001, 0.25], again: null},
        {passed: true, ratios: [0.9999, 0.8], again: null},
      ],
      cursor: 1,
      moment: 0,
    };
    const whole = wholeOf(calm);
    const table = tableOf(whole, whole, null);
    expect(table.columns.map(column => column.index)).toEqual([1]);
    expect(table.defaultSort).toEqual({by: 'objective', index: 1});
    // Lower latency first: 0.25, 0.5, 0.8 of the input, as multiples 4×, 2×, 1.25×.
    expect(table.rows.map(row => row.id)).toEqual(['i1', 'i0', 'i2']);
  });
});

/** `whole` with its last idea still running: it has no reading yet and has not ended. */
function lastIdeaRunning(whole: RunModel): RunModel {
  const last = whole.candidates.candidates.at(-1) as Candidate;
  const running = {...last, outcome: 'running' as const, measurement: null, endedAt: null};
  return {
    ...whole,
    candidates: {
      ...whole.candidates,
      candidates: [...whole.candidates.candidates.slice(0, -1), running],
    },
    series: new Map(
      [...(whole.series ?? [])].map(([name, fold]) => [
        name,
        {...fold, points: fold.points.filter(point => point.id !== last.id)},
      ]),
    ),
  } as RunModel;
}

describe('the position of the time bar', () => {
  test('is the viewed run’s latest time, and none at the run’s end', () => {
    fc.assert(
      fc.property(shapedRuns, each => {
        const whole = wholeOf(each);
        expect(positionOf(whole, whole, 0).scrub).toBeNull();
        const scrub = scrubOf(each);
        const viewed = viewedLikeTheApp(each, whole, scrub);
        expect(positionOf(viewed, whole, 0)).toEqual({whole, scrub});
        expect(scrub).toBeLessThanOrEqual(endOfRun(each));
        expect(inputOf(each, 0) === null).toBe(!each.inputKnown);
      }),
      SETTINGS,
    );
  });
});

/** Core's own runs (`proto/testdata/performance`): the writer's skeleton and head, one objective. */
const corpus = new URL('../../../../../proto/testdata/performance/', import.meta.url);
const cases: readonly {name: string; head: string; skeleton: string}[] = JSON.parse(
  gunzipSync(readFileSync(new URL('cases.json.gz', corpus))).toString(),
);

/** The table of core's run `whole` at sequence `seq`, scrubbed with the app's own projection. */
function expectScrubbedLikeTheChart(skeleton: Skeleton, whole: RunModel, seq: bigint): void {
  const reading = readObjectives(whole)[0] as ObjectiveReading;
  const viewed = projectSkeleton(skeleton, seq);
  const position = positionOf(viewed, whole, clockStart(whole));
  const table = tableOf(viewed, whole, position.scrub);
  const steps = stepsUntil(reading, position.scrub);
  // An idea has a dot exactly when it owns a step of the line the chart draws by then (and the
  // projection has begun the idea: the metric's clock and the span's sequence are not one clock).
  const begun = new Set(viewed.candidates.candidates.map(each => each.id));
  const owners = steps.flatMap(step =>
    step.idea !== null && begun.has(step.idea) ? [step.idea] : [],
  );
  const dotted = table.rows.filter(row => row.cells[0]?.setBest).map(row => row.id);
  expect(new Set(dotted), `at ${seq}`).toEqual(new Set(owners));
  const best = bestSoFar(reading, position.scrub)?.idea;
  const multiple = table.rows.find(row => row.id === best)?.cells[0]?.multiple;
  const stated = heroView(whole, undefined, position.scrub).headlines[0]?.ratio;
  if (multiple != null && stated != null) expect(ratio(multiple)).toBe(stated);
  // The share column is there when the line has climbed from a known input, as in the chart.
  expect(table.shares, `at ${seq}`).toBe(reading.input !== null && steps.length > 0);
}

describe('the Ideas table of a run core wrote, scrubbed with the app’s own projection', () => {
  for (const item of cases) {
    test(`${item.name}: at every position the dots, the best row and the shares are the chart’s`, () => {
      const head = fromBinary(RunHeadSchema, Buffer.from(item.head, 'base64'));
      const skeleton = seedSkeleton(
        fromBinary(KeyframeSchema, Buffer.from(item.skeleton, 'base64')),
      );
      const through = skeleton.throughSeq ?? 0n;
      const whole = projectSkeleton(skeleton, through, '', {head});
      for (let seq = through / 6n; seq <= through; seq += through / 6n + 1n) {
        expectScrubbedLikeTheChart(skeleton, whole, seq);
      }
    });
  }
});

/** `read` with `adopted` named by the run's own decision. */
function adopting(read: RunModel, adopted: Candidate): RunModel {
  return {
    ...read,
    candidates: {
      ...read.candidates,
      best: {candidate: adopted.id, adopted: true},
      adoptionOpened: true,
    },
  } as RunModel;
}

/** The shares add up to the whole; only the adopted idea (the line's last step) can have lost. */
function expectSharesAddUp(whole: RunModel, adopted: Candidate): void {
  const impact = impactOf(whole);
  if (impact.shares === null) return;
  const sum = [...impact.shares.values()].reduce((total, share) => total + share, 0);
  expect(sum + impact.unowned).toBeCloseTo(1, 9);
  for (const [id, share] of impact.shares) {
    if (share <= 0) expect(id).toBe(adopted.id);
  }
}

describe('the Ideas table of a run that adopted an idea that is not its best', () => {
  test('shares add up to the whole, only the adopted idea can lose, and its row states the headline', () => {
    const [, events] = READERS[0];
    fc.assert(
      fc.property(run, each => {
        const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
        const measured = candidates.filter(item => item.measurement !== null);
        const adopted = measured[Math.floor(each.cursor * (measured.length - 1))];
        if (adopted === undefined) return;
        const whole = adopting(events(each, candidates), adopted);
        expectSharesAddUp(whole, adopted);
        const row = tableOf(whole, whole, null).rows.find(each => each.id === adopted.id);
        expect(row?.adopted).toBe(true);
        const multiple = row?.cells[0]?.multiple;
        const stated = heroView(whole).headlines[0]?.ratio;
        if (multiple != null && stated != null && row?.cells[0]?.setBest) {
          expect(ratio(multiple)).toBe(stated);
        }
      }),
      SETTINGS,
    );
  });
});
