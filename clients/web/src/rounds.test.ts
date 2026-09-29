import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {HypothesisEntry, PerformanceContext, RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {
  type RoundRow,
  resultParts,
  retainedText,
  runSummary,
  runTitle,
  shortTitle,
  signed,
  statusLine,
} from './rounds.js';
import {CAPTURED_TYPES} from './session.js';

const DEMO = readFileSync(new URL('./fixtures/demo-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent);
const upTo = (sequence: number) => DEMO.filter(event => (event.sequence ?? 0) <= sequence);
const captured = (events: RunEvent[]) => events.filter(event => CAPTURED_TYPES.has(event.type));
const CONTEXT: PerformanceContext = {
  objective_unit: 'tok/s',
  objective_direction: 'max',
  objective_baseline_value: 950,
  objective_description:
    'Increase decode throughput of the batch inference server without changing outputs. Keep every output token identical.',
};
/** Round 6's judge is working: its execution started (231, 232) and has not finished (233). */
const LIVE = upTo(232);
const liveCore = () => reduceEventBatch(initialCoreState(), LIVE);
const live = () => runSummary(liveCore(), captured(LIVE), [], CONTEXT);
const rowOf = (rows: RoundRow[], round: number): RoundRow => {
  const found = rows.find(row => row.round === round);
  assert.ok(found, `round ${round}`);
  return found;
};
const texts = (row: RoundRow, unit: string | null) =>
  resultParts(row, unit, false).map(part => part.text);

test('rounds come from events alone: titles from the plan, verdicts from round_finished', () => {
  const summary = live();
  assert.deepEqual(
    summary.rows.map(row => [row.round, row.state, row.value, row.delta]),
    [
      [1, 'kept', 1060, 110],
      [2, 'kept', 1150, 90],
      [3, 'reverted', null, null],
      [4, 'kept', 1120, -30],
      [5, 'kept', 1230, 110],
      [6, 'running', 1234, null],
    ],
  );
  assert.equal(rowOf(summary.rows, 1).title, 'Batch decode steps across active sequences');
  assert.match(rowOf(summary.rows, 1).hypothesis ?? '', /^Batching the decode step/);
  assert.deepEqual(rowOf(summary.rows, 3).before, {value: 1150, round: 2});
  assert.deepEqual(summary.retained, {value: 1230, round: 5});
  assert.equal(summary.planned, 6);
  assert.equal(summary.unit, 'tok/s');
});

test('the experiments row outranks events: a discarded candidate is reverted', () => {
  const experiments: HypothesisEntry[] = [
    {
      hypothesis_id: 'H-05',
      title: 'Prefetch KV blocks',
      claim: 'Prefetching hides the copy latency.',
      first_round: 5,
      last_round: 5,
      rounds: [
        {
          round: 5,
          passed: true,
          reviewed: true,
          judge_verdict: 'pass',
          perf_metric: 1230,
          candidate_disposition: 'discard',
        },
      ],
    },
  ];
  const summary = runSummary(liveCore(), captured(LIVE), experiments, CONTEXT);
  assert.equal(rowOf(summary.rows, 5).state, 'reverted');
  assert.equal(rowOf(summary.rows, 5).title, 'Prefetch KV blocks');
  assert.deepEqual(summary.retained, {value: 1120, round: 4});
});

test('the result line: attempted before the verdict, kept or reverted after', () => {
  const {rows, unit} = live();
  assert.deepEqual(texts(rowOf(rows, 6), unit), [
    '1,234 attempted, not yet judged',
    'Retained 1,230',
  ]);
  assert.deepEqual(texts(rowOf(rows, 4), unit), ['Accepted', 'Kept', '1,120 tok/s', '−30']);
  assert.deepEqual(texts(rowOf(rows, 3), unit), [
    'Rejected',
    'Reverted',
    'Not measured',
    'Retained 1,150',
  ]);
  assert.equal(texts(rowOf(rows, 1), unit).at(-1), '+110');
  assert.deepEqual(resultParts(rowOf(rows, 4), unit, false).at(-1), {
    text: '−30',
    tone: 'bad',
    joined: true,
  });
});

test('the status line names the acting phase and a pending command', () => {
  const core = liveCore();
  assert.deepEqual(statusLine(core, null), {
    text: 'Judging round 6',
    busy: false,
    paused: false,
    activeKind: 'judge',
  });
  assert.equal(statusLine(core, 'pause').text, 'Pausing after the current call…');
  assert.equal(statusLine(core, 'stop').busy, true);
});

test('the status line: ended wins over a pending command', () => {
  const ended = reduceEventBatch(initialCoreState(), DEMO);
  assert.deepEqual(statusLine(ended, 'pause'), {
    text: 'Completed',
    busy: false,
    paused: false,
    activeKind: null,
  });
});

test('retained text: the kept checkpoint against the baseline, with a hint', () => {
  assert.deepEqual(retainedText(live()), {
    label: 'Retained',
    value: '1,230',
    unit: 'tok/s',
    change: {text: '+29%', tone: 'ok'},
    hint: 'Kept checkpoint of this run (round 5). Baseline 950 tok/s.',
  });
  assert.equal(retainedText(runSummary(initialCoreState(), [], [], null)), null);
});

test('run title: first sentence of the objective, project from the run input', () => {
  assert.deepEqual(runTitle(CONTEXT, captured(LIVE), 'run-1'), {
    title: 'Increase decode throughput',
    objective: CONTEXT.objective_description,
    project: 'llm-serve',
  });
  assert.equal(runTitle(null, [], 'run-1').title, 'run-1');
});

test('short title: no closing punctuation; a long sentence stops at its first clause break', () => {
  assert.equal(shortTitle('Cut p99 latency.'), 'Cut p99 latency');
  assert.equal(
    shortTitle('Reduce allocator churn in the tokenizer, keeping every output identical!'),
    'Reduce allocator churn in the tokenizer',
  );
  const unbroken = 'Speed up the BPE merge loop in the tokenizer crate substantially';
  assert.equal(shortTitle(unbroken), unbroken);
});

test('the status line: paused after a finished round, in one that has not finished', () => {
  const paused = (events: RunEvent[]) =>
    reduceEventBatch(initialCoreState(), [
      ...events,
      {
        sequence: 10_000,
        type: 'run_status_changed',
        timestamp: '2026-09-25T14:02:00Z',
        data: {kind: 'run_status_changed', status: 'paused', previous: 'pausing'},
      },
    ]);
  assert.equal(statusLine(paused(LIVE), null).text, 'Paused in round 6');
  assert.equal(statusLine(paused(upTo(237)), null).text, 'Paused after round 6');
  const summary = runSummary(paused(LIVE), captured(LIVE), [], CONTEXT);
  assert.equal(rowOf(summary.rows, 6).state, 'running');
});

test('signed values use a real minus sign', () => {
  assert.deepEqual([signed(110), signed(-30), signed(0)], ['+110', '−30', '±0']);
});
