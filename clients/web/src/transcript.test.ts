import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch, type TranscriptEntry} from '@vibesys/core-state';
import {CAPTURED_TYPES, type SentSteer} from './session.js';
import {
  describeTool,
  lineDiff,
  queuedSteers,
  roundTranscript,
  type ToolRow,
  type Turn,
  toolDetail,
} from './transcript.js';

const DEMO = readFileSync(new URL('./fixtures/demo-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent);
const upTo = (sequence: number) => DEMO.filter(event => (event.sequence ?? 0) <= sequence);
const captured = (events: RunEvent[]) => events.filter(event => CAPTURED_TYPES.has(event.type));
const at = (events: RunEvent[], round: number, sent: SentSteer[] = []) => {
  const core = reduceEventBatch(initialCoreState(), events);
  return {
    core,
    model: roundTranscript({core, captured: captured(events), sent, round, runId: null}),
  };
};
const tools = (turn: Turn | undefined): ToolRow[] =>
  turn?.items.flatMap(item => (item.kind === 'tools' ? item.tools : [])) ?? [];
const ev = (
  sequence: number,
  type: RunEvent['type'],
  fields: Partial<RunEvent> = {},
): RunEvent => ({
  sequence,
  type,
  timestamp: `2026-09-28T12:00:${String(sequence).padStart(2, '0')}Z`,
  ...fields,
});
const say = (sequence: number, label: string, kind: string, content: string, id?: string) =>
  ev(sequence, 'agent_output_chunk', {
    round_label: label,
    agent_kind: kind,
    ...(id === undefined ? {} : {execution_id: id, invocation_id: id}),
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  });
const phase = (sequence: number, label: string, kind: string, id?: string) =>
  ev(sequence, 'phase_started', {
    round_label: label,
    agent_kind: kind,
    ...(id === undefined ? {} : {execution_id: id, invocation_id: id}),
    data: {kind: 'phase', phase: kind, attempt: 1},
  });

test('a live round reads as turns: role, phase, and the judge checking the pass criteria', () => {
  const {model} = at(upTo(232), 6);
  assert.deepEqual(
    model.turns.map(turn => [turn.role, turn.phase, turn.active]),
    [
      ['Orchestrator', 'Reviewing the previous round', false],
      ['Orchestrator', 'Planning', false],
      ['Implementer', 'Attempt 1', false],
      ['Judge', 'Attempt 1', true],
    ],
  );
  assert.deepEqual(model.turns.at(-1)?.working, {
    lead: 'Checking the change against the pass criteria:',
    detail: 'Existing correctness tests pass and decode throughput does not regress.',
  });
  // The orchestrator's review says nothing but its result's reasoning.
  assert.equal(model.turns[0]?.items[0]?.kind, 'prose');
});

test('a finished round: one line per tool call, the judge analysis, then the verdict', () => {
  const {model} = at(DEMO, 1);
  const implementer = model.turns.find(turn => turn.kind === 'implementer');
  assert.deepEqual(
    tools(implementer).map(row => row.verb),
    ['Ran', 'Searched', 'Read', 'Edited', 'Ran', 'Searched', 'Edited', 'Ran', 'Ran', 'Ran'],
  );
  const [profile, search, read, , failing] = tools(implementer);
  assert.match(profile?.object ?? '', /^python bench\.py .*….*decode\.prof$/);
  assert.ok((profile?.object ?? '').length <= 58);
  assert.equal(
    profile?.hint,
    'python bench.py --profile --iters 200 --out /tmp/decode.prof\nProfile the decode loop',
  );
  assert.deepEqual([search?.object, search?.detail], ['src/batch.rs', 'for fn decode_step']);
  assert.deepEqual([read?.object, read?.detail], ['src/batch.rs', 'lines 120–170']);
  assert.deepEqual(
    [failing?.object, failing?.end],
    ['cargo test --release', {kind: 'exit', text: 'exit 1'}],
  );
  const judge = model.turns.find(turn => turn.kind === 'judge');
  assert.equal(judge?.items[0]?.kind, 'prose');
  assert.equal(judge?.verdict?.accepted, true);
  assert.match(judge?.verdict?.feedback ?? '', /^The batched launch path/);
  const rejected = at(DEMO, 3).model.turns.find(turn => turn.kind === 'judge');
  assert.equal(rejected?.verdict?.accepted, false);
  assert.equal(model.queued.length, 0);
});

test('tool output: an edit as a diff, a command as its output with failures marked', () => {
  const {core} = at(DEMO, 1);
  const edit = toolDetail(core, '26');
  assert.ok(edit?.kind === 'diff');
  assert.ok(
    edit.lines.some(line => line.tone === 'del' && line.text === 'for seq in seqs.iter_mut() {'),
  );
  const run = toolDetail(core, '28');
  assert.ok(run?.kind === 'output');
  assert.equal(run.command, 'cargo test --release 2>&1 | tail -30');
  assert.ok(
    run.lines.some(line => line.failed && /matches_reference \.\.\. FAILED/.test(line.text)),
  );
  assert.equal(run.pending, false);
  assert.equal(run.cut, null);
  assert.equal(toolDetail(core, 'no-such-row'), null);
});

test('describeTool: verb and object, with the full command in the hint', () => {
  const entry = (toolName: string, toolArguments: Record<string, unknown>): TranscriptEntry => ({
    id: '1',
    kind: 'tool',
    content: '',
    toolName,
    toolArguments,
  });
  const same = (text: string) => text;
  const command =
    'cargo bench --bench decode -- --measurement-time 20 --save-baseline main --noplot';
  const long = describeTool(entry('Bash', {command}), same);
  assert.equal(long.verb, 'Ran');
  assert.match(long.object ?? '', /^cargo bench .*….*--noplot$/);
  assert.equal(long.hint, command);
  const edit = describeTool(
    entry('Edit', {file_path: 'src/lib.rs', old_string: 'a\nb', new_string: 'c'}),
    same,
  );
  assert.deepEqual(
    [edit.verb, edit.object, edit.added, edit.removed],
    ['Edited', 'src/lib.rs', 1, 2],
  );
  const write = describeTool(entry('Write', {file_path: 'src/q.rs', content: 'x\ny\n'}), same);
  assert.deepEqual([write.verb, write.added, write.removed], ['Wrote', 2, null]);
  const grep = describeTool(entry('Grep', {pattern: 'RequestQueue', path: 'src/queue.rs'}), same);
  assert.deepEqual(
    [grep.verb, grep.object, grep.detail],
    ['Searched', 'src/queue.rs', 'for RequestQueue'],
  );
});

test('prompts come from the execution start; todos from the agent latest list', () => {
  const label = 'round-2-retry-1-implementer';
  const scope = {
    round_label: label,
    agent_kind: 'implementer',
    execution_id: 'x1',
    invocation_id: 'x1',
  };
  const events = [
    ev(1, 'agent_execution_started', {
      ...scope,
      data: {
        kind: 'agent_execution_started',
        stage: 'implementer',
        attempt: 1,
        user_prompt: 'Task: batch the decode step',
        activity: {kind: 'agent_execution_activity_changed', mode: 'thinking', summary: 'Working'},
      },
    }),
    ev(2, 'phase_started', {...scope, data: {kind: 'phase', phase: 'implementer', attempt: 1}}),
    ev(3, 'todo_update', {
      ...scope,
      data: {
        kind: 'todo_update',
        todos: [
          {content: 'Run the tests', status: 'completed'},
          {content: 'Run the benchmark', status: 'pending'},
        ],
      },
    }),
  ];
  const turn = at(events, 2).model.turns[0];
  assert.equal(turn?.id, 'x1');
  assert.equal(turn?.prompt, 'Task: batch the decode step');
  assert.deepEqual(
    turn?.todos.map(todo => [todo.content, todo.status]),
    [
      ['Run the tests', 'completed'],
      ['Run the benchmark', 'pending'],
    ],
  );
});

test('events without execution ids form one turn per kind and attempt', () => {
  const events = [
    phase(1, 'round-1-retry-1-implementer', 'implementer'),
    say(2, 'round-1-retry-1-implementer', 'implementer', 'Looking at the loop.'),
    phase(3, 'round-1-retry-2-implementer', 'implementer'),
    say(4, 'round-1-retry-2-implementer', 'implementer', 'Trying again.'),
    phase(5, 'round-1-retry-2-judge', 'judge'),
    say(6, 'round-1-retry-2-judge', 'judge', 'Reviewing.'),
  ];
  const {model} = at(events, 1);
  assert.deepEqual(
    model.turns.map(turn => [turn.role, turn.phase, turn.items.length]),
    [
      ['Implementer', 'Attempt 1', 1],
      ['Implementer', 'Attempt 2', 1],
      ['Judge', 'Attempt 2', 1],
    ],
  );
  assert.equal(new Set(model.turns.map(turn => turn.id)).size, 3);
});

test('two executions under one label stay two turns; an invocation alias joins its execution', () => {
  const label = 'round-1-retry-1-implementer';
  const events = [
    phase(1, label, 'implementer', 'a'),
    phase(2, label, 'implementer', 'b'),
    say(3, label, 'implementer', 'From a.', 'a'),
    say(4, label, 'implementer', 'From b.', 'b'),
  ];
  assert.deepEqual(
    at(events, 1).model.turns.map(turn => [turn.id, turn.items.length]),
    [
      ['a', 1],
      ['b', 1],
    ],
  );
  const aliased = [
    ev(1, 'agent_execution_started', {
      round_label: label,
      agent_kind: 'implementer',
      execution_id: 'exec-1',
      invocation_id: 'inv-1',
      data: {
        kind: 'agent_execution_started',
        stage: 'implementer',
        activity: {mode: 'thinking', summary: 'Working'},
      },
    }),
    ev(2, 'agent_output_chunk', {
      round_label: label,
      agent_kind: 'implementer',
      invocation_id: 'inv-1',
      data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'Aliased.'},
    }),
  ];
  assert.deepEqual(
    at(aliased, 1).model.turns.map(turn => [turn.id, turn.items.length]),
    [['exec-1', 1]],
  );
});

test('without ids, a repeated label is a new execution at each start, prompts included', () => {
  const label = 'round-1-retry-1-implementer';
  const start = (sequence: number, prompt: string) =>
    ev(sequence, 'agent_execution_started', {
      round_label: label,
      agent_kind: 'implementer',
      data: {
        kind: 'agent_execution_started',
        stage: 'implementer',
        user_prompt: prompt,
        activity: {mode: 'thinking', summary: 'Working'},
      },
    });
  const finish = (sequence: number) =>
    ev(sequence, 'agent_execution_finished', {
      round_label: label,
      agent_kind: 'implementer',
      data: {kind: 'agent_execution_finished'},
    });
  const events = [
    start(1, 'First prompt'),
    phase(2, label, 'implementer'),
    say(3, label, 'implementer', 'One.'),
    finish(4),
    start(5, 'Second prompt'),
    phase(6, label, 'implementer'),
    say(7, label, 'implementer', 'Two.'),
  ];
  const {model} = at(events, 1);
  assert.deepEqual(
    model.turns.map(turn => [turn.id, turn.prompt, turn.items.length]),
    [
      [`implementer|${label}`, 'First prompt', 1],
      [`implementer|${label}#1`, 'Second prompt', 1],
    ],
  );
});

test('entries whose phase events fell below the floor still form turns', () => {
  const label = 'round-2-retry-1-implementer';
  const events = [
    say(40, label, 'implementer', 'Half way.', 'x7'),
    say(41, label, 'implementer', ' Done.', 'x7'),
  ];
  const {model} = at(events, 2);
  assert.deepEqual(
    model.turns.map(turn => [turn.id, turn.role, turn.phase]),
    [['x7', 'Implementer', 'Attempt 1']],
  );
});

test('steers: applied inside the consuming turn, queued until consumed', () => {
  const label = 'round-1-retry-1-judge';
  const events = [
    phase(1, label, 'judge'),
    say(2, label, 'judge', 'Reviewing.'),
    ev(3, 'control', {
      status: 'pending',
      text: '/steer: Measure first',
      agent_kind: 'judge',
      round_label: label,
    }),
    ev(4, 'control', {status: 'consumed', text: '/steer', agent_kind: 'judge', round_label: label}),
  ];
  const {model} = at(events, 1, [{id: 'sent-2', text: 'Hold on', afterSequence: 4}]);
  assert.deepEqual(model.turns[0]?.items.at(-1), {
    kind: 'steer',
    id: 'steer-3',
    text: 'Measure first',
  });
  assert.deepEqual(model.queued, [{id: 'sent-2', text: 'Hold on'}]);
  // Consumed after it was sent: no longer queued, whether or not its pending event was seen.
  assert.deepEqual(
    queuedSteers(captured(events), [{id: 'sent-1', text: 'Measure first', afterSequence: 2}]),
    [],
  );
  assert.deepEqual(
    queuedSteers(captured(events), [{id: 'sent-1', text: 'Late', afterSequence: 2}]),
    [],
  );
});

test('a consumed steer lands in the execution its control event names', () => {
  const label = 'round-1-retry-1-implementer';
  const events = [
    phase(1, label, 'implementer', 'a'),
    phase(2, label, 'implementer', 'b'),
    say(3, label, 'implementer', 'From a.', 'a'),
    say(4, label, 'implementer', 'From b.', 'b'),
    ev(5, 'control', {
      status: 'pending',
      text: '/steer: Only a',
      agent_kind: 'implementer',
      round_label: label,
    }),
    ev(6, 'control', {
      status: 'consumed',
      text: '/steer',
      agent_kind: 'implementer',
      round_label: label,
      execution_id: 'a',
    }),
  ];
  const {model} = at(events, 1);
  assert.deepEqual(
    model.turns.map(turn => [turn.id, turn.items.at(-1)?.kind]),
    [
      ['a', 'steer'],
      ['b', 'prose'],
    ],
  );
});

test('repeated identical steers keep one identity each, whatever arrives first', () => {
  const pending = (sequence: number) =>
    ev(sequence, 'control', {
      status: 'pending',
      text: '/steer: Go',
      agent_kind: 'judge',
      round_label: 'round-1-retry-1-judge',
    });
  const sent: SentSteer[] = [
    {id: 'sent-1', text: 'Go', afterSequence: 4},
    {id: 'sent-2', text: 'Go', afterSequence: 4},
  ];
  // Both acknowledged, no event yet.
  assert.deepEqual(queuedSteers([], sent), [
    {id: 'sent-1', text: 'Go'},
    {id: 'sent-2', text: 'Go'},
  ]);
  // One event arrived: the first sent steer claims it and keeps its id.
  assert.deepEqual(queuedSteers([pending(5)], sent), [
    {id: 'sent-1', text: 'Go'},
    {id: 'sent-2', text: 'Go'},
  ]);
  // Both events arrived before the second acknowledgment was recorded.
  assert.deepEqual(queuedSteers([pending(5), pending(6)], sent.slice(0, 1)), [
    {id: 'sent-1', text: 'Go'},
    {id: 'steer-6', text: 'Go'},
  ]);
  assert.deepEqual(queuedSteers([pending(5), pending(6)], sent), [
    {id: 'sent-1', text: 'Go'},
    {id: 'sent-2', text: 'Go'},
  ]);
});

test('lineDiff keeps the shared prefix and suffix as context', () => {
  assert.deepEqual(lineDiff('a\nb\nc', 'a\nx\nc'), [
    {tone: 'ctx', text: 'a', line: null},
    {tone: 'del', text: 'b', line: null},
    {tone: 'add', text: 'x', line: null},
    {tone: 'ctx', text: 'c', line: null},
  ]);
});
