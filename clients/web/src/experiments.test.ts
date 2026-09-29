import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {DesignRound, HypothesisEntry, RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {chartModel, designRows, evidenceRows} from './experiments.js';
import {type RoundRow, type RunSummary, runSummary} from './rounds.js';
import {CAPTURED_TYPES} from './session.js';

const LIVE = readFileSync(new URL('./fixtures/demo-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent)
  .filter(event => (event.sequence ?? 0) <= 232);
const captured = LIVE.filter(event => CAPTURED_TYPES.has(event.type));
const summary = runSummary(reduceEventBatch(initialCoreState(), LIVE), captured, [], {
  objective_unit: 'tok/s',
  objective_baseline_value: 950,
});
const DESIGN: DesignRound[] = [
  {
    round: 3,
    base: 'c0ffee02',
    commit: 'c0ffee03',
    files: [{path: 'src/sampler.rs', change: 'modified'}],
  },
];

test('the chart: the retained step line, a mark per round, planned rounds, the axis', () => {
  const chart = chartModel(summary, 12);
  assert.ok(chart);
  assert.deepEqual(
    chart.points.map(point => [point.round, point.mark]),
    [
      [1, 'kept'],
      [2, 'kept'],
      [3, 'unmeasured'],
      [4, 'kept'],
      [5, 'kept'],
      [6, 'judging'],
    ],
  );
  assert.equal(chart.points[2]?.y, chart.floor);
  assert.equal(chart.points[5]?.label, 'Round 6: 1,234 tok/s, not yet judged');
  assert.deepEqual(
    chart.planned.map(point => point.round),
    [7, 8, 9, 10, 11, 12],
  );
  assert.equal(chart.ticks.length, 12);
  assert.equal(chart.path.match(/ V/g)?.length, 4);
  assert.deepEqual([chart.baseline?.value, chart.retained?.value], ['950', '1,230']);
  assert.deepEqual(
    [chart.baseline?.label, chart.retained?.label],
    ['Baseline: 950 tok/s', 'Retained: 1,230 tok/s'],
  );
  assert.equal(chartModel(runSummary(initialCoreState(), [], [], null), 12), null);
});

test('evidence without experiments: outcomes from verdicts, facts from the recorded plan', () => {
  const rows = evidenceRows(summary, [], DESIGN, captured);
  assert.deepEqual(
    rows.map(row => [row.round, row.outcome.text, row.outcome.tone]),
    [
      [1, 'Kept', 'ok'],
      [2, 'Kept', 'ok'],
      [3, 'Rejected', 'bad'],
      [4, 'Kept', 'ok'],
      [5, 'Kept', 'ok'],
      [6, 'Judging', 't2'],
    ],
  );
  assert.deepEqual([rows[0]?.value, rows[0]?.valueLabel], ['1,060', '1,060 tok/s']);
  assert.equal(rows[2]?.valueLabel, null);
  const terms = rows[2]?.facts.map(fact => fact.term);
  assert.deepEqual(terms, ['Hypothesis', 'Pass criteria', 'Judge', 'Change', 'Files', 'Commit']);
  assert.deepEqual(rows[2]?.facts.at(-1), {term: 'Commit', text: 'c0ffee03', mono: true});
});

test('evidence with experiments: the recorded hypothesis outcome', () => {
  const experiments: HypothesisEntry[] = [
    {
      hypothesis_id: 'H-03',
      first_round: 3,
      last_round: 3,
      rounds: [
        {round: 3, passed: false, reviewed: true, hypothesis_outcome: 'implementation_failed'},
      ],
    },
  ];
  assert.deepEqual(evidenceRows(summary, experiments, DESIGN, captured)[2]?.outcome, {
    text: 'Failed',
    tone: 'bad',
  });
});

test('design rows: files and changed lines per round, reverted rounds marked, the live round once it edits', () => {
  const edits = new Map([
    [3, new Map([['src/sampler.rs', {added: 2, removed: 1}]])],
    [6, new Map([['src/queue.rs', {added: 54, removed: 0}]])],
  ]);
  assert.deepEqual(
    designRows(summary, DESIGN, captured, edits).map(row => [
      row.round,
      row.files,
      row.reverted,
      row.stat,
    ]),
    [
      [3, 'src/sampler.rs', true, {added: 2, removed: 1}],
      [6, 'src/queue.rs', false, {added: 54, removed: 0}],
    ],
  );
});

test('the judge fact is the judge finding, not the verdict rationale the transcript shows', () => {
  const judge = evidenceRows(summary, [], DESIGN, captured)[2]?.facts.find(
    fact => fact.term === 'Judge',
  );
  assert.ok(judge);
  assert.match(judge.text, /^Ran the full suite against the reverted staging-buffer patch/);
});

function kept(round: number, value: number): RoundRow {
  return {
    round,
    state: 'kept',
    title: null,
    hypothesis: null,
    value,
    delta: null,
    before: {round: round - 1, value: null},
  };
}

function summaryOf(rows: RoundRow[], baseline: number | null): RunSummary {
  const last = rows.at(-1);
  return {
    rows,
    unit: 'ms',
    baseline,
    retained: {round: last?.round ?? 0, value: last?.value ?? baseline},
    planned: null,
    lowerIsBetter: true,
  };
}

function assertInside(chart: ReturnType<typeof chartModel>) {
  assert.ok(chart);
  const ys = [...chart.points.map(point => point.y), chart.baseline?.y, chart.retained?.y];
  for (const y of ys.filter(y => y !== undefined)) {
    assert.ok(Number.isFinite(y) && y >= 0 && y <= chart.floor, `y ${y} outside the plot`);
  }
  assert.doesNotMatch(chart.path, /NaN|Infinity/);
  return chart;
}

test('the chart: a single point with no baseline sits inside the plot', () => {
  const chart = assertInside(chartModel(summaryOf([kept(1, 42)], null), null));
  assert.equal(chart.points.length, 1);
  assert.equal(chart.ticks.length, 1);
  assert.equal(chart.baseline, null);
  assert.equal(chart.points[0]?.label, 'Round 1: 42 ms');
});

test('the chart: all-equal values share one height', () => {
  const chart = assertInside(chartModel(summaryOf([kept(1, 100), kept(2, 100)], 100), 4));
  assert.deepEqual(
    new Set([chart.baseline?.y, chart.retained?.y, ...chart.points.map(point => point.y)]).size,
    1,
  );
});

test('the chart: a zero value is plotted, not treated as missing', () => {
  const chart = assertInside(chartModel(summaryOf([kept(1, 0)], 0), 2));
  assert.equal(chart.points[0]?.mark, 'kept');
  assert.equal(chart.baseline?.label, 'Baseline: 0 ms');
});
