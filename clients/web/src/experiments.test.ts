import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {DesignRound, HypothesisEntry, RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {chartModel, designRows, evidenceRows} from './experiments.js';
import {runSummary} from './rounds.js';
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

test('design rows: files per round, reverted rounds marked', () => {
  assert.deepEqual(
    designRows(summary, DESIGN, captured).map(row => [row.round, row.files, row.reverted]),
    [[3, 'src/sampler.rs', true]],
  );
});
