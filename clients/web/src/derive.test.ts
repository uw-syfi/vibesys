import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {
  DesignRound,
  HypothesisEntry,
  PerformanceContext,
  PerformanceRound,
  RunEvent,
} from '@vibesys/backend-client/browser';
import {
  type AgentPhase,
  type CoreState,
  initialCoreState,
  reduceEventBatch,
} from '@vibesys/core-state';
import {
  agentGraph,
  announce,
  endedWord,
  formatDelta,
  formatDuration,
  formatValue,
  headerModel,
  inspectorModel,
  layoutGraph,
  logGroups,
  NODE,
  needsOlder,
  pathShortener,
  prose,
  railModel,
  railState,
  runControl,
  showsChanges,
  steers,
  steersNeedOlder,
  trendModel,
} from './derive.js';
import type {AgentGraph, Connection, GraphNode, LogItem, PlacedNode} from './model.js';
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

// The stub run's performance series: rounds 3 and 6 recorded no value, so they have no row.
const SERIES: PerformanceRound[] = [1000, 1045, null, 1135, 1180, null, 1270, 1315].flatMap(
  (value, index) =>
    value === null
      ? []
      : [{round: index + 1, perf_metric: value, perf_unit: 'median_tok_per_sec', passed: true}],
);

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

test('the trend plots one point per measured round, spaced by round number', () => {
  const model = trendModel(SERIES, STUB_CONTEXT);
  assert.ok(model);
  assert.equal(model.points.length, 6, 'rounds 3 and 6 recorded no value, so they have no point');
  // x follows the round number, not the position: a skipped round stays a gap, never interpolated.
  assert.deepEqual(
    model.points.map(point => point.x),
    [3, 16.43, 43.29, 56.71, 83.57, 97],
  );
  // y is inverted and scaled to the plotted values: the lowest sits at the bottom inset.
  assert.equal(model.points[0]?.y, 32);
  assert.equal(model.points.at(-1)?.y, 4);
  assert.deepEqual([model.first, model.last], ['1K', '1.315K'], 'the rail rows write them so');
  // The name says the span the curve covers, so no count can disagree with the rows.
  assert.deepEqual([model.firstRound, model.lastRound], [1, 8]);
  // One row without a number would otherwise poison every coordinate.
  const bogus = [...SERIES, {round: 9, perf_metric: Number.NaN, perf_unit: '', passed: true}];
  assert.equal(trendModel(bogus, STUB_CONTEXT)?.points.length, 6);
});

test('the trend starts at R0 only when the objective records a baseline', () => {
  assert.equal(trendModel(SERIES, STUB_CONTEXT)?.points.length, 6);
  const model = trendModel(SERIES, BASELINE);
  assert.equal(model?.points.length, 7);
  assert.equal(model?.first, '900');
  assert.deepEqual([model?.firstRound, model?.lastRound], [0, 8], 'the span starts at R0');
  assert.equal(model?.points[0]?.x, 3, 'R0 is the leftmost point');
  assert.equal(model?.points[0]?.y, 32, 'and the lowest, so the curve rises out of it');
});

test('a trend needs two points; one measured round alone draws nothing', () => {
  assert.equal(trendModel([], BASELINE), null);
  assert.equal(trendModel(SERIES.slice(0, 1), STUB_CONTEXT), null);
  assert.equal(trendModel(SERIES.slice(0, 1), BASELINE)?.points.length, 2);
});

test('a flat metric draws a flat line at mid height', () => {
  const flat = SERIES.map(row => ({...row, perf_metric: 1000}));
  assert.deepEqual(
    trendModel(flat, STUB_CONTEXT)?.points.map(point => point.y),
    [18, 18, 18, 18, 18, 18],
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
    groups.map(group => [group.role, group.collapsed, group.active, group.calls]),
    [
      ['orchestrator', true, false, 37],
      ['implementer', false, true, 69],
    ],
  );
  assert.equal(groups[0]?.summary, 'Roadmap written. Plan below.');
  const items = groups[1]?.items ?? [];
  assert.deepEqual(tool(items, '273'), {
    kind: 'tool',
    id: '273',
    verb: 'Read effective objective',
    arg: 'cat …/runtime/effective-objective.md',
    argFull: `cat .vibesys/state/runs/${QUEUE_RUN}/runtime/effective-objective.md`,
    result: null,
    duration: '0.3s',
    inFlight: false,
  });
  assert.equal(tool(items, '487')?.verb, 'Wrote');
  assert.equal(tool(items, '487')?.arg, 'src/lib.rs');
  assert.equal(tool(items, '487')?.argFull, null, 'a row that shows its whole target has no tip');
  assert.deepEqual(tool(items, '516')?.result, {text: '12 passed', failed: false});
  assert.equal(tool(items, '623')?.verb, 'Measure baseline vs ring, 3 reps each');
  assert.equal(tool(items, '623')?.inFlight, true);
  assert.equal(items.filter(item => item.kind === 'tool' && item.inFlight).length, 1);
  const orchestrator = groups[0]?.items ?? [];
  assert.equal(
    tool(orchestrator, '49')?.argFull,
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

test('tool rows: a command carries its wall clock, and nothing else does', () => {
  const rows = logGroups(fold(QUEUE), [], 1, QUEUE_RUN)
    .flatMap(group => group.items)
    .filter(item => item.kind === 'tool');
  const timed = rows.filter(row => row.duration !== null);
  assert.equal(timed.length, 105, 'every command payload of the recording reports one');
  assert.ok(rows.length > timed.length, 'a result without a command payload reports none');
  // The payload decides, not the tool name: the recording times four non-Bash calls whose
  // results came back as command payloads.
  assert.ok(timed.some(row => row.verb === 'Wrote'));
  // Tenths under ten seconds, whole seconds under a minute: `0:00` would say nothing about a
  // call that took 40 ms, and the recording's slowest is 52.8 s.
  const shapes = [...new Set(timed.map(row => (row.duration ?? '').replace(/\d/g, '0')))].sort();
  assert.deepEqual(shapes, ['0.0s', '00s']);
  assert.ok(
    timed.some(row => row.duration === '53s'),
    'the 52.8 s build rounds to whole seconds',
  );
});

test('tool rows: a command row names its executable and the first path it touched', () => {
  const rows = logGroups(fold(QUEUE), [], 1, QUEUE_RUN)
    .flatMap(group => group.items)
    .filter(item => item.kind === 'tool');
  const args = rows.flatMap(row => (row.arg === null ? [] : [row.arg]));
  assert.ok(args.length > 80, `the fixture has command rows (${args.length})`);
  // No row repeats its verb in shell. Two segments is the floor, so the rows still past 40
  // characters are the ones with a 51-character run id or a long library name inside a segment;
  // cutting inside a segment is the ellipsis's job, which knows the real width.
  assert.deepEqual(
    args.filter(arg => arg.length > 40),
    [
      `ls …/runs/${QUEUE_RUN}`,
      `echo …/${QUEUE_RUN}/run.json`,
      'export …/release/libqueue_candidate.dylib',
    ],
  );
  // A path with more than three segments keeps its last two; a shallow one stays whole.
  assert.ok(args.includes('make queue-candidate.so'), 'an executable plus what it built');
  assert.ok(args.includes('nm …/deps/libqueue_candidate.dylib'), 'a deep path keeps two segments');
  assert.ok(args.includes('cat src/lib.rs'), 'a shallow path stays whole');
  // An inline script is the executable and nothing else: its body is not a target.
  assert.ok(args.includes('python3'), 'a heredoc names no path');
  // The whole command stays reachable wherever the row shows less than all of it.
  for (const row of rows) {
    if (row.argFull !== null) assert.ok(row.argFull.length > (row.arg?.length ?? 0));
  }
});

test('log rows: adjacent calls of one verb fold into one counted row', () => {
  const base = {
    round_label: 'round-1-pre',
    agent_kind: 'orchestrator',
    invocation_id: 'x',
    execution_id: 'x',
    timestamp: '2026-09-21T12:00:00Z',
  };
  const call = (sequence: number, tool: string, args: Record<string, string>) =>
    ({
      ...base,
      sequence,
      type: 'tool_call',
      data: {kind: 'tool_call', tool, call_id: `c${sequence}`, args},
    }) as RunEvent;
  const back = (sequence: number, tool: string, error: boolean) =>
    ({
      ...base,
      sequence,
      type: 'tool_result',
      data: {
        kind: 'tool_result',
        tool,
        call_id: `c${sequence - 1}`,
        content: 'ok',
        is_error: error,
      },
    }) as RunEvent;
  const read = (sequence: number, file: string, error = false) => [
    call(sequence, 'Read', {file_path: file}),
    back(sequence + 1, 'Read', error),
  ];
  const events: RunEvent[] = [
    {
      ...base,
      sequence: 1,
      type: 'agent_execution_started',
      data: {
        kind: 'agent_execution_started',
        stage: 'pre',
        activity: {mode: 'tool', summary: 'working'},
      },
    } as RunEvent,
    ...Array.from({length: 15}, (_, index) => read(10 + index * 2, `a${index}.rs`)).flat(),
    ...read(40, 'broken.rs', true),
    ...read(44, 'b0.rs'),
    ...read(46, 'b1.rs'),
    // The open call at the live edge: it is not folded, so the row in flight stays a row.
    call(50, 'Read', {file_path: 'b2.rs'}),
  ];
  const items = logGroups(fold(events), [], 1, null).flatMap(group => group.items);
  assert.deepEqual(
    items.map(item =>
      item.kind === 'run' ? ['run', item.verb, item.items.length] : [item.kind, item.id],
    ),
    [
      ['run', 'Read', 15],
      ['tool', '40'],
      ['run', 'Read', 2],
      ['tool', '50'],
    ],
    'a failure and the call in flight each stand alone; everything adjacent to them folds',
  );
  const first = items[0];
  assert.equal(first?.kind === 'run' && first.items[3]?.arg, 'a3.rs', 'members keep their target');
  assert.equal(
    logGroups(fold(events), [], 1, null)[0]?.calls,
    19,
    'the group still counts every call, folded or not',
  );
  assert.deepEqual(
    logGroups(fold(QUEUE), [], 1, QUEUE_RUN)
      .flatMap(group => group.items)
      .filter(item => item.kind === 'run'),
    [],
    'the recording shells out for everything, so no two adjacent calls share a verb',
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

test('log groups: every group of a retry carries its attempt number', () => {
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
    logGroups(core, [], 1, null).map(group => [group.role, group.attempt]),
    [
      ['orchestrator', 1],
      ['implementer', 1],
      ['judge', 1],
      ['implementer', 2],
      ['judge', 2],
    ],
    'the badge sits on each header, so both groups of attempt 2 say which attempt made them',
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
    logGroups(reprompted, [], 1, null).map(group => [group.role, group.attempt]),
    [
      ['orchestrator', 1],
      ['implementer', 1],
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
    connection: 'connected' as const,
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
    announce(pulse('running', 8), {...pulse('completed', 8), ended: 'Completed'}),
    'Run completed',
  );
});

test('announcements: a dropped connection says Reconnecting once; connecting says nothing', () => {
  // '' clears the region without speech; null leaves it as it is.
  const at = (connection: Connection, status: CoreState['status'] = 'running') => ({
    status,
    round: status === 'connecting' ? null : 5,
    ended: null,
    connection,
  });
  assert.equal(announce(at('connected'), at('disconnected')), 'Reconnecting…');
  assert.equal(announce(at('disconnected'), at('disconnected')), null, 'not again while down');
  assert.equal(
    announce(at('disconnected'), at('connected')),
    '',
    'back online clears the region silently, so the next drop is announced again',
  );
  assert.equal(announce(at('connecting', 'connecting'), at('connected', 'connecting')), '');
  assert.equal(
    announce(at('connected', 'connecting'), at('disconnected', 'connecting')),
    'Reconnecting…',
    'a drop before the first batch folds',
  );
  assert.equal(
    announce(at('connecting'), at('disconnected')),
    null,
    'a failed dial after Retry is the alert banner, not a drop',
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

const phase = (patch: Partial<AgentPhase> & Pick<AgentPhase, 'kind'>): AgentPhase => ({
  status: 'pending',
  roundNumber: 1,
  roundLabel: null,
  ...patch,
});
const withPhases = (phases: AgentPhase[]): CoreState => ({...initialCoreState(), phases});
const roles = (core: CoreState, round: number | null) =>
  agentGraph(core, round).nodes.map(node => node.role);
const statuses = (core: CoreState, round: number | null) =>
  agentGraph(core, round).nodes.map(node => node.status);
/** Each edge as `[source role, target role, tone]`, which reads better than execution ids. */
const wires = (core: CoreState, round: number | null) => {
  const graph = agentGraph(core, round);
  const role = new Map(graph.nodes.map(node => [node.id, node.role]));
  return graph.edges.map(edge => [role.get(edge.from), role.get(edge.to), edge.tone]);
};

test('agent graph: nodes in loop order, pending roles, runtime, and the inferred chain', () => {
  // queue-rs at 623: the orchestrator has run twice, the implementer is working.
  const core = fold(upTo(QUEUE, 623));
  assert.deepEqual(roles(core, 1), ['Orchestrator', 'Implementer', 'Judge', 'Profiler']);
  // The loop runs the orchestrator twice a round; both calls say the same thing, so they collapse
  // into one counted card (the stacking cases are in the collapse test below).
  assert.deepEqual(statuses(core, 1), ['completed', 'active', 'pending', 'pending']);
  const graph = agentGraph(core, 1);
  assert.equal(graph.nodes[1]?.runtime, 'claude-opus-5');
  assert.equal(graph.nodes[1]?.runtimeTip, 'Claude Code (claude-opus-5)');
  // Judge and profiler are pending because run_started advertised them: run-map seeds them from
  // core.expectedRoles, and nothing here re-derives that list.
  assert.deepEqual(core.expectedRoles, ['orchestrator', 'implementer', 'judge', 'profiler']);
  assert.equal(graph.nodes[2]?.runtime, null);
  // The backend reports no edges, so the shape is the chain the kinds imply: one edge per pair of
  // adjacent kinds, and none out of the last one.
  assert.deepEqual(
    graph.edges.map(edge => [edge.from, edge.to]),
    [
      [graph.nodes[0]?.id, graph.nodes[1]?.id],
      [graph.nodes[1]?.id, graph.nodes[2]?.id],
      [graph.nodes[2]?.id, graph.nodes[3]?.id],
    ],
  );
});

test('agent graph: edge tones for live, idle, done, and failed, per edge', () => {
  // Active on either end is live; an edge into a kind that has not started is idle.
  assert.deepEqual(wires(fold(upTo(QUEUE, 623)), 1), [
    ['Orchestrator', 'Implementer', 'live'],
    ['Implementer', 'Judge', 'live'],
    ['Judge', 'Profiler', 'idle'],
  ]);
  // The run was interrupted inside the implementer, which fails its phase.
  assert.deepEqual(wires(fold(QUEUE), 1), [
    ['Orchestrator', 'Implementer', 'done'],
    ['Implementer', 'Judge', 'failed'],
    ['Judge', 'Profiler', 'idle'],
  ]);
  // A finished round hands over all the way down.
  assert.deepEqual(wires(fold(STUB), 3), [
    ['Orchestrator', 'Implementer', 'done'],
    ['Implementer', 'Judge', 'done'],
  ]);
});

test('agent graph: a retried kind chains its own cards, and only the last hands over', () => {
  // An orchestrator that failed and ran again is two cards. The loop handed over once, so there is
  // one edge into the implementer; wiring both cards to it would claim a handover that never was.
  const core = withPhases([
    phase({kind: 'orchestrator', status: 'failed', model: 'claude-opus-5'}),
    phase({kind: 'orchestrator', status: 'completed', model: 'claude-opus-5'}),
    phase({kind: 'implementer', status: 'active', model: 'gpt-5.1-codex-max'}),
  ]);
  assert.deepEqual(wires(core, 1), [
    ['Orchestrator', 'Orchestrator', 'failed'],
    ['Orchestrator', 'Implementer', 'live'],
  ]);
  // One edge per hop, never one card count times the next.
  const graph = agentGraph(core, 1);
  assert.equal(graph.edges.length, graph.nodes.length - 1);
});

test('agent graph: the card shows the model, the tooltip pairs it with the harness', () => {
  const core = withPhases([
    phase({kind: 'implementer', status: 'active', provider: 'codex', model: 'gpt-5.1-codex-max'}),
    phase({kind: 'judge', provider: 'stub'}),
    phase({kind: 'profiler', model: 'gemini-3-pro'}),
    phase({kind: 'perf_eval'}),
  ]);
  const graph = agentGraph(core, 1);
  // Model and harness: the model shows, the pair goes to the tooltip. Either alone shows alone
  // and needs no tooltip. Neither shows no runtime line.
  assert.deepEqual(
    graph.nodes.map(node => [node.runtime, node.runtimeTip]),
    [
      ['gpt-5.1-codex-max', 'Codex (gpt-5.1-codex-max)'],
      ['Stub', null],
      ['gemini-3-pro', null],
      [null, null],
    ],
  );
  assert.deepEqual(
    graph.nodes.map(node => node.role),
    ['Implementer', 'Judge', 'Profiler', 'Perf eval'],
  );
});

test('agent graph: adjacent agents that say the same thing collapse into one counted card', () => {
  const run = (patches: Array<Partial<AgentPhase>>) =>
    agentGraph(
      withPhases(patches.map(patch => phase({kind: 'orchestrator', ...patch}))),
      1,
    ).nodes.map(node => [node.status, node.runtime, node.count]);
  const claude = {provider: 'claude', model: 'claude-opus-5'};
  const opus = 'claude-opus-5';
  // Two identical agents are one fact stated twice.
  assert.deepEqual(
    run([
      {status: 'completed', ...claude},
      {status: 'completed', ...claude},
    ]),
    [['completed', opus, 2]],
  );
  assert.deepEqual(
    run([
      {status: 'completed', ...claude},
      {status: 'completed', ...claude},
      {status: 'completed', ...claude},
    ]),
    [['completed', opus, 3]],
  );
  // A different status or a different model is a different fact: both stack.
  assert.deepEqual(
    run([
      {status: 'completed', ...claude},
      {status: 'active', ...claude},
    ]),
    [
      ['completed', opus, 1],
      ['active', opus, 1],
    ],
  );
  assert.deepEqual(
    run([
      {status: 'completed', ...claude},
      {status: 'completed', provider: 'claude', model: 'claude-sonnet-5'},
    ]),
    [
      ['completed', opus, 1],
      ['completed', 'claude-sonnet-5', 1],
    ],
  );
  // The card shows only the model, so the same model on two harnesses must not merge.
  assert.deepEqual(
    run([
      {status: 'completed', ...claude},
      {status: 'completed', provider: 'codex', model: 'claude-opus-5'},
    ]),
    [
      ['completed', opus, 1],
      ['completed', opus, 1],
    ],
  );
  // Only adjacent agents collapse: an odd one out keeps the two around it apart.
  assert.deepEqual(
    run([
      {status: 'completed', ...claude},
      {status: 'failed', ...claude},
      {status: 'completed', ...claude},
    ]),
    [
      ['completed', opus, 1],
      ['failed', opus, 1],
      ['completed', opus, 1],
    ],
  );
  // The recorded run: both of the round's orchestrator calls completed on the same harness.
  assert.deepEqual(
    agentGraph(fold(upTo(QUEUE, 623)), 1).nodes.map(node => node.count),
    [2, 1, 1, 1],
  );
});

test('agent graph: a round with no phases yields nothing', () => {
  const empty: AgentGraph = {nodes: [], edges: []};
  assert.deepEqual(agentGraph(fold(STUB), 0), empty);
  assert.deepEqual(agentGraph(fold(STUB), null), empty);
  assert.deepEqual(agentGraph(initialCoreState(), 1), empty);
});

// --- layout ------------------------------------------------------------------------------------

const box = (id: string, status: AgentPhase['status'] = 'completed'): GraphNode => ({
  id,
  role: id,
  status,
  runtime: null,
  runtimeTip: null,
  count: 1,
});
const graphOf = (ids: string[], wired: Array<[string, string]>): AgentGraph => ({
  nodes: ids.map(id => box(id)),
  edges: wired.map(([from, to]) => ({from, to, tone: 'done' as const})),
});
const at = (nodes: PlacedNode[], id: string): PlacedNode => {
  const found = nodes.find(node => node.id === id);
  if (found === undefined) throw new Error(`no node ${id}`);
  return found;
};
const overlap = (left: PlacedNode, right: PlacedNode) =>
  left.x < right.x + NODE.width &&
  right.x < left.x + NODE.width &&
  left.y < right.y + NODE.height &&
  right.y < left.y + NODE.height;
/** Every point along a polyline, about one per pixel: no box is 1px wide, so nothing slips past. */
const walk = (points: Array<{x: number; y: number}>) => {
  const out: Array<{x: number; y: number}> = [];
  for (let index = 1; index < points.length; index++) {
    const from = points[index - 1] as {x: number; y: number};
    const to = points[index] as {x: number; y: number};
    const steps = Math.max(1, Math.ceil(Math.hypot(to.x - from.x, to.y - from.y)));
    for (let step = 0; step <= steps; step++) {
      out.push({
        x: from.x + ((to.x - from.x) * step) / steps,
        y: from.y + ((to.y - from.y) * step) / steps,
      });
    }
  }
  return out;
};
const within = (point: {x: number; y: number}, node: PlacedNode) =>
  point.x > node.x + 0.5 &&
  point.x < node.x + NODE.width - 0.5 &&
  point.y > node.y + 0.5 &&
  point.y < node.y + NODE.height - 0.5;
/** A point on the target's border, which is where dagre clips the last segment. */
const lands = (point: {x: number; y: number}, node: PlacedNode) => {
  const inside =
    point.x >= node.x - 1 &&
    point.x <= node.x + NODE.width + 1 &&
    point.y >= node.y - 1 &&
    point.y <= node.y + NODE.height + 1;
  const onBorder =
    Math.abs(point.x - node.x) <= 1 ||
    Math.abs(point.x - node.x - NODE.width) <= 1 ||
    Math.abs(point.y - node.y) <= 1 ||
    Math.abs(point.y - node.y - NODE.height) <= 1;
  return inside && onBorder;
};

/** Every shape must place its boxes apart and route every edge to its target, around the rest. */
const readable = (graph: AgentGraph, what: string) => {
  const layout = layoutGraph(graph);
  assert.equal(layout.nodes.length, graph.nodes.length, `${what}: a box per node`);
  assert.deepEqual(
    layout.nodes.map(node => node.id),
    graph.nodes.map(node => node.id),
    `${what}: source order stays loop order`,
  );
  for (const [index, node] of layout.nodes.entries()) {
    for (const other of layout.nodes.slice(index + 1)) {
      assert.equal(overlap(node, other), false, `${what}: ${node.id} overlaps ${other.id}`);
    }
    assert.ok(node.x >= 0 && node.y >= 0, `${what}: ${node.id} is off the canvas`);
    assert.ok(
      node.x + NODE.width <= layout.width + 0.5 && node.y + NODE.height <= layout.height + 0.5,
      `${what}: ${node.id} is outside the measured canvas`,
    );
  }
  assert.equal(layout.edges.length, graph.edges.length, `${what}: an edge per wire`);
  for (const edge of layout.edges) {
    const target = at(layout.nodes, edge.to);
    assert.ok(edge.points.length >= 2, `${what}: ${edge.from} to ${edge.to} has no path`);
    const end = edge.points.at(-1) as {x: number; y: number};
    assert.ok(lands(end, target), `${what}: ${edge.from} to ${edge.to} misses its target`);
    for (const point of walk(edge.points)) {
      for (const node of layout.nodes) {
        if (node.id === edge.from || node.id === edge.to) continue;
        assert.equal(within(point, node), false, `${what}: that edge crosses ${node.id}`);
      }
    }
  }
  return layout;
};

test('graph layout: a chain runs left to right on one row', () => {
  const layout = readable(
    graphOf(
      ['a', 'b', 'c'],
      [
        ['a', 'b'],
        ['b', 'c'],
      ],
    ),
    'chain',
  );
  const [a, b, c] = [at(layout.nodes, 'a'), at(layout.nodes, 'b'), at(layout.nodes, 'c')];
  assert.equal(a.y, b.y);
  assert.equal(b.y, c.y);
  assert.ok(b.x - a.x === NODE.width + 32 && c.x - b.x === NODE.width + 32);
  // One row of cards, so the panel is one card tall and the log keeps the rest.
  assert.equal(layout.height, NODE.height);
});

test('graph layout: a fan-in of two parents stacks them into one child', () => {
  const layout = readable(
    graphOf(
      ['p', 'q', 'c'],
      [
        ['p', 'c'],
        ['q', 'c'],
      ],
    ),
    'fan-in',
  );
  const [p, q, c] = [at(layout.nodes, 'p'), at(layout.nodes, 'q'), at(layout.nodes, 'c')];
  assert.equal(p.x, q.x, 'both parents share a rank');
  assert.ok(c.x > p.x, 'the child is one rank to the right');
  assert.ok(
    Math.abs(p.y - q.y) >= NODE.height,
    'the parents are stacked, not on top of each other',
  );
});

test('graph layout: a fan-out of three children stacks them past one parent', () => {
  const layout = readable(
    graphOf(
      ['a', 'x', 'y', 'z'],
      [
        ['a', 'x'],
        ['a', 'y'],
        ['a', 'z'],
      ],
    ),
    'fan-out',
  );
  const children = ['x', 'y', 'z'].map(id => at(layout.nodes, id));
  const parent = at(layout.nodes, 'a');
  for (const child of children) assert.ok(child.x > parent.x);
  assert.equal(new Set(children.map(child => child.y)).size, 3, 'three rows of children');
  assert.equal(layout.height, NODE.height * 3 + 12 * 2, 'three node rows plus two gaps');
});

test('graph layout: a skip edge routes around the node it skips', () => {
  // a to c jumps a rank. The `readable` check is the point: it must not cross b's box.
  const layout = readable(
    graphOf(
      ['a', 'b', 'c'],
      [
        ['a', 'b'],
        ['b', 'c'],
        ['a', 'c'],
      ],
    ),
    'skip edge',
  );
  assert.equal(layout.nodes.length, 3);
});

test('graph layout: two disconnected chains keep their own rows', () => {
  const layout = readable(
    graphOf(
      ['a', 'b', 'c', 'd'],
      [
        ['a', 'b'],
        ['c', 'd'],
      ],
    ),
    'two chains',
  );
  const row = (id: string) => at(layout.nodes, id).y;
  assert.equal(row('a'), row('b'));
  assert.equal(row('c'), row('d'));
  assert.notEqual(row('a'), row('c'), 'the two chains are on different rows');
});
