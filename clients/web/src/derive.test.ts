import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {
  DesignRound,
  HypothesisEntry,
  PerformanceContext,
  RunEvent,
} from '@vibesys/backend-client/browser';
import {type CoreState, initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {
  announce,
  endedWord,
  formatDelta,
  formatDuration,
  formatValue,
  headerModel,
  inspectorModel,
  logGroups,
  needsOlder,
  pathShortener,
  prose,
  railModel,
  railState,
  runControl,
  showsChanges,
  steers,
  steersNeedOlder,
} from './derive.js';
import type {LogItem} from './model.js';
import {CAPTURED_TYPES} from './session.js';

const read = (path: string): string => readFileSync(new URL(path, import.meta.url), 'utf8');
const jsonl = (path: string): RunEvent[] =>
  read(path)
    .split('\n')
    .filter(Boolean)
    .map(line => JSON.parse(line) as RunEvent);
// A real agent run: typed tool payloads, one round, interrupted at sequence 627.
const QUEUE = jsonl('../../tui/dev/fixtures/queue-rs-payloads.jsonl');
const QUEUE_RUN = '20260831-210421-dad182f4-queue-rs-20260831-210421';
// A real stub run: 8 rounds, one pause, one steer (pending 212, consumed 215), completed.
const STUB = jsonl('./fixtures/stub-run.jsonl');
const STUB_EXPERIMENTS = JSON.parse(read('./fixtures/stub-experiments.json')) as HypothesisEntry[];
const STUB_DESIGN = JSON.parse(read('./fixtures/stub-design.json')) as DesignRound[];
// The stub run's objective has a description and nothing else.
const STUB_CONTEXT = JSON.parse(read('./fixtures/stub-performance.json')) as PerformanceContext;
// No recording has a baseline value; this one exists to exercise R0.
const BASELINE: PerformanceContext = {
  ...STUB_CONTEXT,
  objective_metric: 'median_tok_per_sec',
  objective_unit: 'median_tok_per_sec',
  objective_direction: 'max',
  objective_baseline_value: 900,
};

const upTo = (events: RunEvent[], sequence: number) =>
  events.filter(event => (event.sequence ?? 0) <= sequence);
const fold = (events: RunEvent[]): CoreState => reduceEventBatch(initialCoreState(), events);
const captured = (events: RunEvent[]) => events.filter(event => CAPTURED_TYPES.has(event.type));
/** The store after a tail bootstrap at `floor`: only events above it are folded and captured. */
const tail = (events: RunEvent[], floor: number) => {
  const above = events.filter(event => (event.sequence ?? 0) > floor);
  return {core: {...fold(above), historyAfterSequence: floor}, held: captured(above)};
};
const before = (round: number) => STUB_EXPERIMENTS.filter(entry => entry.last_round < round);
const tool = (items: LogItem[], id: string) => {
  const item = items.find(candidate => candidate.id === id);
  assert.equal(item?.kind, 'tool', `tool row ${id}`);
  return item?.kind === 'tool' ? item : null;
};

test('formats compact values, durations, and signed deltas', () => {
  assert.deepEqual([6_210_000, 112_700_000, 1045].map(formatValue), ['6.21M', '112.7M', '1.045K']);
  assert.deepEqual([0, 374_000, 6_198_000].map(formatDuration), ['0:00', '6:14', '1:43:18']);
  assert.deepEqual([1.14, -2.06, 0].map(formatDelta), ['+1.1%', '-2.1%', '0.0%']);
});

test('rail rows: statuses, values, official, incumbent, rounds left', () => {
  const ended = railModel(fold(STUB), STUB_EXPERIMENTS, STUB_CONTEXT);
  assert.deepEqual(
    ended.rows.map(row => [row.round, row.status, row.value, row.incumbent]),
    [
      [1, 'kept', '1K', false],
      [2, 'kept', '1.045K', false],
      [3, 'kept', null, false],
      [4, 'kept', '1.135K', false],
      [5, 'kept', '1.18K', false],
      [6, 'kept', null, false],
      [7, 'kept', '1.27K', false],
      [8, 'kept', '1.315K', true],
    ],
  );
  assert.equal(ended.rows[0]?.valueTip, '1,000 median_tok_per_sec');
  assert.equal(
    ended.rows.some(row => row.official),
    false,
    'the stub run has no official values',
  );
  assert.equal(ended.roundsLeft, null, 'an ended run has no budget line');

  const paused = railModel(fold(upTo(STUB, 205)), before(3), STUB_CONTEXT);
  assert.deepEqual(
    paused.rows.map(row => [row.round, row.status, row.incumbent, row.live !== null]),
    [
      [1, 'kept', false, false],
      [2, 'kept', true, false],
      [3, 'paused', false, true],
    ],
  );
  assert.equal(paused.roundsLeft, 5, 'max_rounds 8 minus round 3');

  const official = STUB_EXPERIMENTS.map(entry => ({
    ...entry,
    rounds: (entry.rounds ?? []).map(round =>
      round.round === 2 ? {...round, official_evaluation: true} : round,
    ),
  }));
  assert.deepEqual(
    railModel(fold(STUB), official, STUB_CONTEXT)
      .rows.filter(row => row.official)
      .map(row => row.round),
    [2],
  );

  const outcomes = STUB_EXPERIMENTS.map(entry => ({
    ...entry,
    rounds: (entry.rounds ?? []).map(round =>
      round.round === 7
        ? {...round, passed: false}
        : round.round === 8
          ? {...round, judge_verdict: 'fail' as const}
          : round,
    ),
  }));
  const judged = railModel(fold(STUB), outcomes, STUB_CONTEXT).rows;
  assert.deepEqual(
    judged.slice(-3).map(row => [row.round, row.status, row.incumbent]),
    [
      [6, 'kept', false],
      [7, 'failed', false],
      [8, 'rejected', false],
    ],
  );
  assert.equal(judged.find(row => row.incumbent)?.round, 5, 'the latest kept round with a value');

  const interrupted = railModel(fold(QUEUE), [], null);
  assert.deepEqual(
    interrupted.rows.map(row => [row.round, row.status]),
    [[1, 'failed']],
    'the run ended inside round 1',
  );
});

test('rail state: loading, unattached, ready, and a failed first load', () => {
  const experiments = (ready: boolean) => ({request_id: 'q', ok: true, experiments_ready: ready});
  assert.equal(railState(null, null), 'loading');
  assert.equal(railState(experiments(false), null), 'unattached');
  assert.equal(railState(experiments(true), null), 'ready');
  assert.equal(railState(null, 'Experiments unavailable'), 'error', 'the error alone, no skeleton');
  assert.equal(railState(experiments(true), 'down'), 'ready', 'a failed refetch keeps its rows');
});

test('R0: a baseline row only with a baseline value, the incumbent until the first kept round', () => {
  assert.equal(
    railModel(fold(STUB), STUB_EXPERIMENTS, STUB_CONTEXT).rows[0]?.round,
    1,
    'no baseline value, no R0',
  );
  const early = railModel(fold(upTo(STUB, 60)), [], BASELINE);
  assert.deepEqual(
    early.rows.map(row => [row.round, row.status, row.value, row.incumbent]),
    [
      [0, 'baseline', '900', true],
      [1, 'running', null, false],
    ],
  );
  assert.equal(early.rows[0]?.valueTip, '900 median_tok_per_sec');
  const pending = inspectorModel(early.rows, [], [], captured(upTo(STUB, 60)), 1, BASELINE);
  assert.deepEqual(pending.delta, {value: null, vs: 0, tip: null}, 'Pending vs R0');
  assert.deepEqual(pending.metric, {name: 'median_tok_per_sec', direction: 'max'});

  const rows = railModel(fold(STUB), STUB_EXPERIMENTS, BASELINE).rows;
  assert.deepEqual(
    rows.filter(row => row.incumbent).map(row => row.round),
    [8],
    'a kept round takes over',
  );
  const r1 = inspectorModel(rows, STUB_EXPERIMENTS, STUB_DESIGN, captured(STUB), 1, BASELINE);
  assert.deepEqual(r1.delta, {value: '+11.1%', vs: 0, tip: '1K vs 900 median_tok_per_sec'});
  const r0 = inspectorModel(rows, STUB_EXPERIMENTS, STUB_DESIGN, captured(STUB), 0, BASELINE);
  assert.deepEqual(
    [r0.hypothesis, r0.delta, r0.judge, r0.changes],
    [null, null, [], null],
    'R0 has no hypothesis, delta, verdicts, or changes',
  );
});

test('run control comes from core.status and the connection only', () => {
  const at = (status: CoreState['status']): CoreState => ({...initialCoreState(), status});
  const control = (status: CoreState['status'], connection: 'connected' | 'disconnected') => {
    const value = runControl(at(status), [], connection);
    return value.kind === 'action' ? [value.action, value.label, value.disabled] : [value.word];
  };
  assert.deepEqual(control('running', 'connected'), ['pause', 'Pause', false]);
  assert.deepEqual(control('pausing', 'connected'), ['pause', 'Pausing', true]);
  assert.deepEqual(control('paused', 'connected'), ['resume', 'Resume', false]);
  assert.deepEqual(control('starting', 'connected'), ['pause', 'Pause', true]);
  assert.deepEqual(control('connecting', 'connected'), ['pause', 'Pause', true]);
  assert.deepEqual(control('running', 'disconnected'), ['pause', 'Pause', true]);
  assert.deepEqual(control('paused', 'disconnected'), ['resume', 'Resume', true]);
  assert.deepEqual(control('completed', 'connected'), ['Completed']);
  assert.deepEqual(control('failed', 'disconnected'), ['Failed']);
  assert.equal(endedWord(fold(QUEUE), captured(QUEUE)), 'Interrupted');
  assert.equal(endedWord(fold(STUB), captured(STUB)), 'Completed');
  const interrupted = runControl(fold(QUEUE), captured(QUEUE), 'connected');
  assert.deepEqual(interrupted, {
    kind: 'ended',
    word: 'Interrupted',
    tip: 'RuntimeError: launcher_terminated (SIGTERM)',
  });
});

test('header: project basename, run clock bounds', () => {
  const live = headerModel(fold(upTo(QUEUE, 623)), captured(upTo(QUEUE, 623)), 'connected', null);
  assert.equal(live.project, 'queue-rs');
  assert.equal(live.startedAt, '2026-08-31T21:04:21.526240Z');
  assert.equal(live.endedAt, null);
  assert.equal(live.objective, null, 'no objective without a performance context');
  const ended = headerModel(fold(QUEUE), captured(QUEUE), 'connected', null);
  assert.equal(ended.endedAt, '2026-08-31T21:27:07.460497Z');
  assert.deepEqual(headerModel(fold(STUB), captured(STUB), 'connected', STUB_CONTEXT).objective, {
    first: 'Speed up the VALUE computation in candidate.py.',
    full: 'Speed up the VALUE computation in candidate.py.',
  });
  const long = {objective_description: 'Optimize a queue.\n\nKeep the C ABI. Stay linearizable.'};
  assert.deepEqual(headerModel(fold(STUB), [], 'connected', long).objective, {
    first: 'Optimize a queue.',
    full: 'Optimize a queue.\n\nKeep the C ABI. Stay linearizable.',
  });
});

test('header objective: soft wraps joined, paragraphs and list items kept, backticks stripped', () => {
  const wrapped = {
    objective_description: [
      'Optimize a bounded SPSC',
      'queue. Metric: `total_ops_per_sec`.',
      '',
      'Preserve the interface:',
      '- Provide `./queue-candidate.so`.',
      '* Export the copying',
      '  C ABI.',
      '1. Stay',
      'linearizable.',
    ].join('\n'),
  };
  assert.deepEqual(headerModel(fold(STUB), [], 'connected', wrapped).objective, {
    first: 'Optimize a bounded SPSC queue.',
    full: [
      'Optimize a bounded SPSC queue. Metric: total_ops_per_sec.',
      '',
      'Preserve the interface:',
      '- Provide ./queue-candidate.so.',
      '* Export the copying C ABI.',
      '1. Stay linearizable.',
    ].join('\n'),
  });
});

test('steers: pending until a consumed control event, then placed at the consuming call', () => {
  assert.deepEqual(steers(captured(upTo(STUB, 212))), {
    pending: [{id: 'steer-212', text: 'Try caching VALUE instead of recomputing it.'}],
    consumed: [],
  });
  const {pending, consumed} = steers(captured(STUB));
  assert.deepEqual(pending, []);
  assert.deepEqual(consumed, [
    {
      id: 'steer-212',
      sequence: 215,
      text: 'Try caching VALUE instead of recomputing it.',
      round: 3,
      roundLabel: 'round-3-retry-1-implementer',
      agentKind: 'implementer',
    },
  ]);
  const groups = logGroups(fold(STUB), consumed, 3, null);
  assert.deepEqual(
    groups.map(group => [group.role, group.collapsed, group.items.map(item => item.kind)]),
    [['implementer', false, ['steer']]],
    'the steer opens the implementer group; the empty orchestrator and judge groups are dropped',
  );
  for (let round = 1; round <= 8; round++) {
    assert.ok(
      logGroups(fold(STUB), consumed, round, null).every(group => group.items.length > 0),
      `R${round} renders no role group without entries`,
    );
  }
});

test('log groups: role groups, collapse, in-flight row, tool rows from typed payloads', () => {
  const core = fold(upTo(QUEUE, 623));
  const groups = logGroups(core, [], 1, QUEUE_RUN);
  assert.deepEqual(
    groups.map(group => [group.role, group.collapsed, group.active, group.calls, group.divider]),
    [
      ['orchestrator', true, false, 37, null],
      ['implementer', false, true, 69, null],
    ],
  );
  assert.equal(groups[0]?.summary, 'Roadmap written. Plan below.');
  const items = groups[1]?.items ?? [];
  assert.deepEqual(tool(items, '273'), {
    kind: 'tool',
    id: '273',
    verb: 'Read effective objective',
    arg: `cat .vibesys/state/runs/${QUEUE_RUN}/runtime/effective-objective.md`,
    result: null,
    inFlight: false,
  });
  assert.equal(tool(items, '487')?.verb, 'Wrote');
  assert.equal(tool(items, '487')?.arg, 'src/lib.rs');
  assert.deepEqual(tool(items, '516')?.result, {text: '12 passed', failed: false});
  assert.equal(tool(items, '623')?.verb, 'Measure baseline vs ring, 3 reps each');
  assert.equal(tool(items, '623')?.inFlight, true);
  assert.equal(items.filter(item => item.kind === 'tool' && item.inFlight).length, 1);
  const orchestrator = groups[0]?.items ?? [];
  assert.equal(
    tool(orchestrator, '49')?.arg,
    `cat .vibesys/state/runs/${QUEUE_RUN}/runtime/effective-objective.md`,
    'the absolute workspace prefix is stripped',
  );
  assert.equal(
    logGroups(fold(QUEUE), [], 1, QUEUE_RUN).some(group =>
      group.items.some(item => item.kind === 'tool' && item.inFlight),
    ),
    false,
    'nothing is in flight once the run ended',
  );
});

test('log groups: the acting role shows before its first entry', () => {
  // queue-rs at 263: the implementer has started and said nothing yet.
  const groups = logGroups(fold(upTo(QUEUE, 263)), [], 1, QUEUE_RUN);
  assert.deepEqual(
    groups.map(group => [group.role, group.collapsed, group.active, group.items.length === 0]),
    [
      ['orchestrator', true, false, false],
      ['implementer', false, true, true],
    ],
  );
});

test('log groups: an "Attempt N" divider marks only the start of a retry', () => {
  const event = (sequence: number, type: RunEvent['type'], label: string, kind: string) =>
    ({
      sequence,
      type,
      round_label: label,
      agent_kind: kind,
      timestamp: '2026-09-21T12:00:00Z',
      ...(type === 'agent_output_chunk'
        ? {data: {kind: 'agent_output_chunk', channel: 'analysis', content: `said ${sequence}`}}
        : {}),
    }) as RunEvent;
  const core = fold([
    event(1, 'phase_started', 'round-1-plan', 'orchestrator'),
    event(2, 'agent_output_chunk', 'round-1-plan', 'orchestrator'),
    event(3, 'phase_started', 'round-1-retry-1-implementer', 'implementer'),
    event(4, 'agent_output_chunk', 'round-1-retry-1-implementer', 'implementer'),
    event(5, 'phase_started', 'round-1-retry-1-judge', 'judge'),
    event(6, 'agent_output_chunk', 'round-1-retry-1-judge', 'judge'),
    event(7, 'phase_started', 'round-1-retry-2-implementer', 'implementer'),
    event(8, 'agent_output_chunk', 'round-1-retry-2-implementer', 'implementer'),
    event(9, 'phase_started', 'round-1-retry-2-judge', 'judge'),
    event(10, 'agent_output_chunk', 'round-1-retry-2-judge', 'judge'),
  ]);
  assert.deepEqual(
    logGroups(core, [], 1, null).map(group => [group.role, group.attempt, group.divider]),
    [
      ['orchestrator', 1, null],
      ['implementer', 1, null],
      ['judge', 1, null],
      ['implementer', 2, 2],
      ['judge', 2, null],
    ],
  );
  // A plan reprompt (`round-N-retry-K-plan`) retries the plan call, not the round.
  const reprompted = fold([
    event(1, 'phase_started', 'round-1-plan', 'orchestrator'),
    event(2, 'agent_output_chunk', 'round-1-plan', 'orchestrator'),
    event(3, 'phase_started', 'round-1-retry-1-plan', 'orchestrator'),
    event(4, 'agent_output_chunk', 'round-1-retry-1-plan', 'orchestrator'),
    event(5, 'phase_started', 'round-1-retry-12-plan', 'orchestrator'),
    event(6, 'agent_output_chunk', 'round-1-retry-12-plan', 'orchestrator'),
    event(7, 'phase_started', 'round-1-retry-1-implementer', 'implementer'),
    event(8, 'agent_output_chunk', 'round-1-retry-1-implementer', 'implementer'),
  ]);
  assert.deepEqual(
    logGroups(reprompted, [], 1, null).map(group => [group.role, group.attempt, group.divider]),
    [
      ['orchestrator', 1, null],
      ['implementer', 1, null],
    ],
  );
});

test('inspector: hypothesis, delta vs the incumbent of that time, judge verdict words, changes', () => {
  const core = fold(STUB);
  const rows = railModel(core, STUB_EXPERIMENTS, STUB_CONTEXT).rows;
  const r4 = inspectorModel(rows, STUB_EXPERIMENTS, STUB_DESIGN, captured(STUB), 4, STUB_CONTEXT);
  assert.deepEqual(
    r4.hypothesis,
    {
      id: 'H-04',
      title: 'batching the prefill step removes per-request launch overhead',
      claim: null,
    },
    'a title derived from the claim is not shown twice',
  );
  assert.deepEqual(r4.metric, {name: 'median_tok_per_sec', direction: null});
  const named = {...STUB_CONTEXT, objective_metric: 'tok_per_sec'};
  assert.deepEqual(
    inspectorModel(rows, STUB_EXPERIMENTS, STUB_DESIGN, captured(STUB), 4, named).metric,
    {name: 'tok_per_sec', direction: null},
    'objective_metric names the metric before the round unit does',
  );
  assert.deepEqual(r4.delta, {
    value: '+8.6%',
    vs: 2,
    tip: '1.135K vs 1.045K median_tok_per_sec',
  });
  assert.deepEqual(r4.judge, [{attempt: 1, verdict: 'Kept', feedback: '', open: true}]);
  assert.deepEqual(r4.changes, {commit: STUB_DESIGN[3]?.commit, files: []});

  const live = fold(upTo(STUB, 205));
  const r3 = inspectorModel(
    railModel(live, before(3), STUB_CONTEXT).rows,
    before(3),
    STUB_DESIGN.slice(0, 2),
    captured(upTo(STUB, 205)),
    3,
    STUB_CONTEXT,
  );
  assert.deepEqual(r3.delta, {value: null, vs: 2, tip: null}, 'Pending vs R2');
  assert.equal(r3.changes, null, 'no changes while the round runs');
  assert.deepEqual(r3.judge, []);

  const judged = [
    ...captured(STUB),
    {
      sequence: 900,
      type: 'judge_result',
      round_label: 'round-9-retry-1',
      timestamp: '2026-09-21T12:00:00Z',
      data: {
        kind: 'judge_result',
        verdict: 'fail',
        feedback: 'Dropped 8 of 257 bytes.',
        attempt: 1,
      },
    },
    {
      sequence: 901,
      type: 'judge_result',
      round_label: 'round-9-retry-2',
      timestamp: '2026-09-21T12:00:00Z',
      data: {
        kind: 'judge_result',
        verdict: 'pass',
        feedback: 'Not worth a second path.',
        attempt: 2,
      },
    },
  ] as RunEvent[];
  // Round 9's experiments row discards the candidate, which is what makes its rail row rejected.
  const discarded: HypothesisEntry[] = [
    {
      hypothesis_id: 'H-09',
      first_round: 9,
      last_round: 9,
      rounds: [{round: 9, passed: true, reviewed: true, candidate_disposition: 'discard'}],
    },
  ];
  const rejected = [...rows, {...rows[0], round: 9, status: 'rejected' as const, incumbent: false}];
  assert.deepEqual(
    inspectorModel(rejected as typeof rows, discarded, [], judged, 9, null).judge.map(attempt => [
      attempt.attempt,
      attempt.verdict,
      attempt.open,
    ]),
    [
      [1, 'Gate failed', false],
      [2, 'Rejected', true],
    ],
    'the verdict is a word; a passed attempt under a rejected round reads Rejected',
  );
});

test('inspector: without an experiments row, a passing final attempt reads Passed, not Kept', () => {
  // Unattached, or finished before the experiments refetch: no row says kept or rejected.
  const rows = railModel(fold(STUB), [], STUB_CONTEXT).rows;
  assert.deepEqual(inspectorModel(rows, [], [], captured(STUB), 4, STUB_CONTEXT).judge, [
    {attempt: 1, verdict: 'Passed', feedback: '', open: true},
  ]);
});

test('inspector: the metric heads a delta, never stands alone', () => {
  const rows = railModel(fold(STUB), STUB_EXPERIMENTS, BASELINE).rows;
  const at = (round: number) =>
    inspectorModel(rows, STUB_EXPERIMENTS, STUB_DESIGN, captured(STUB), round, BASELINE);
  assert.deepEqual(
    [0, 1, 3].map(round => [round, at(round).metric?.name ?? null, at(round).delta?.value ?? null]),
    [
      [0, null, null],
      [1, 'median_tok_per_sec', '+11.1%'],
      [3, null, null],
    ],
    'R0 and a round without a value have no delta, so no metric heading',
  );
});

test('changes: only a finished round past R0 shows them, or their load error', () => {
  const rows = railModel(fold(upTo(STUB, 205)), before(3), BASELINE).rows;
  assert.deepEqual(
    rows.map(row => [row.round, showsChanges(row)]),
    [
      [0, false],
      [1, true],
      [2, true],
      [3, false],
    ],
  );
  assert.equal(showsChanges(undefined), false);
});

test('announcements: status changes and new rounds, nothing on first load', () => {
  const pulse = (status: CoreState['status'], round: number | null, ended = null) => ({
    status,
    round,
    ended,
  });
  assert.equal(announce(null, pulse('running', 5)), null);
  assert.equal(announce(pulse('connecting', null), pulse('running', 5)), null);
  assert.equal(announce(pulse('running', 5), pulse('running', 5)), null);
  assert.equal(
    announce(pulse('running', 5), pulse('pausing', 5)),
    'Pausing after the current agent call',
  );
  assert.equal(announce(pulse('pausing', 5), pulse('paused', 5)), 'Paused');
  assert.equal(announce(pulse('paused', 5), pulse('running', 5)), 'Running');
  assert.equal(announce(pulse('running', 5), pulse('running', 6)), 'Round 6 started');
  assert.equal(
    announce(pulse('running', 8), {status: 'completed', round: 8, ended: 'Completed'}),
    'Run completed',
  );
});

test('backfill is offered only where the tail floor may hide a round', () => {
  const core = fold(upTo(STUB, 205));
  assert.equal(needsOlder(core, 1), false, 'full history');
  const tailed = {...core, historyAfterSequence: 120};
  assert.equal(needsOlder(tailed, 1), true);
  assert.equal(needsOlder(tailed, 2), true, "round 1's replayed round_finished is below the floor");
  assert.equal(needsOlder(tailed, 3), false);
  assert.equal(needsOlder(tailed, 0), false, 'R0 has no events to load');
});

test('queued steers: backfill for any started round reaches the latest call start', () => {
  // The stub at 212: paused in round 3, a steer queued after the plan call started at 190.
  const paused = upTo(STUB, 212);
  for (const floor of [212, 195, 189]) {
    assert.equal(needsOlder(tail(paused, floor).core, 3), true, `floor ${floor}`);
  }
  const {core, held} = tail(paused, 150);
  assert.equal(needsOlder(core, 3), false, "round 2's judge call is above the floor");
  assert.deepEqual(
    steers(held).pending.map(steer => steer.text),
    ['Try caching VALUE instead of recomputing it.'],
  );
});

test('consumed steers: backfill reaches the start of the call before the consuming call', () => {
  // The stub's steer: queued during round 3's plan call (190), consumed at 215.
  assert.equal(steersNeedOlder(tail(STUB, 200).core, tail(STUB, 200).held, 3), true);
  assert.equal(steersNeedOlder(tail(STUB, 189).core, tail(STUB, 189).held, 3), false);

  const event = (
    sequence: number,
    type: RunEvent['type'],
    label: string | null,
    kind: string | null,
    extra: Partial<RunEvent> = {},
  ) =>
    ({
      sequence,
      type,
      round_label: label,
      agent_kind: kind,
      timestamp: '2026-09-21T12:00:00Z',
      ...extra,
    }) as RunEvent;
  const said = (sequence: number, label: string, kind: string) =>
    event(sequence, 'agent_output_chunk', label, kind, {
      data: {kind: 'agent_output_chunk', channel: 'analysis', content: `said ${sequence}`},
    });
  const queuedAt = event(11, 'control', null, null, {status: 'pending', text: '/steer: Check it.'});
  const consumedAt = (sequence: number) =>
    event(sequence, 'control', 'round-2-pre', 'orchestrator', {status: 'consumed', text: '/steer'});
  // Queued while round 1's judge ran, consumed by round 2's first call.
  const crossing = [
    event(10, 'phase_started', 'round-1-retry-1-judge', 'judge'),
    queuedAt,
    said(12, 'round-1-retry-1-judge', 'judge'),
    consumedAt(20),
    event(21, 'phase_started', 'round-2-pre', 'orchestrator'),
    said(22, 'round-2-pre', 'orchestrator'),
  ];
  const hidden = tail(crossing, 11);
  assert.equal(steersNeedOlder(hidden.core, hidden.held, 0), false, 'R0 consumed nothing');
  assert.equal(needsOlder(hidden.core, 2), false, "round 1's judge output is above the floor");
  assert.equal(steersNeedOlder(hidden.core, hidden.held, 2), true, 'its start is not');
  assert.deepEqual(steers(hidden.held).consumed, [], 'so the steer text is missing');
  const shown = tail(crossing, 9);
  assert.equal(steersNeedOlder(shown.core, shown.held, 2), false);
  assert.deepEqual(
    steers(shown.held).consumed.map(steer => [steer.round, steer.text]),
    [[2, 'Check it.']],
  );
  // The consuming call's own start can come before its control event; it does not count.
  const started = [
    ...crossing.slice(0, 3),
    event(20, 'phase_started', 'round-2-pre', 'orchestrator'),
    consumedAt(21),
    said(22, 'round-2-pre', 'orchestrator'),
  ];
  assert.equal(steersNeedOlder(tail(started, 11).core, tail(started, 11).held, 2), true);
  assert.equal(steersNeedOlder(tail(started, 9).core, tail(started, 9).held, 2), false);
});

test('prose keeps paragraphs, inline code, and bold; paths shorten at word starts only', () => {
  assert.deepEqual(prose('One `ldar` per **dequeue**.\n\nNext.'), [
    [
      {kind: 'text', text: 'One '},
      {kind: 'code', text: 'ldar'},
      {kind: 'text', text: ' per '},
      {kind: 'strong', text: 'dequeue'},
      {kind: 'text', text: '.'},
    ],
    [{kind: 'text', text: 'Next.'}],
  ]);
  const short = pathShortener('run-7');
  assert.equal(short("cat '/w/run-7/src/a.rs' /x/run-7/b"), "cat 'src/a.rs' b");
  assert.equal(short('cat .vibesys/runs/run-7/x'), 'cat .vibesys/runs/run-7/x');
});
