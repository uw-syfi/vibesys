import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import * as fc from 'fast-check';
import {type RunPageAction, type RunPageState, runPageInitial, runPageStep} from './run-page.js';
import {type TurnRow, turnTranscript} from './turn-transcript.js';
import {viewedRun} from './views.js';

/** A run's turns as the server sends them: span events, then each turn's events stamped with it. */

const RUN = {host: 'ssh:host-b', runId: '20261015-163047'} as const;

function span(
  id: string,
  parent: string | null,
  kind: string,
  title: string,
  status: Span['status'] = 'running',
  at = 1,
): Span {
  return {
    id,
    parent,
    type: parent === null ? 'flow' : 'operation',
    name: kind,
    status,
    started_at: at,
    label: {kind, title, attrs_json: '{}'},
  };
}

function started(sequence: number, spanned: Span): RunEvent {
  return {
    ...event(sequence, 'span_started'),
    data: {kind: 'span_started', span: spanned},
  } as RunEvent;
}

/** The trace of one planning call with two workstreams of one agent turn each, and a gate. */
const SPANS: readonly Span[] = [
  span('flow:root#1', null, 'root', 'dynamic'),
  span('flow:root/plan#1', 'flow:root#1', 'planning_call', 'planning call 1'),
  span('flow:root/ws-1#1', 'flow:root#1', 'workstream', 'Pad the counters'),
  span('flow:root/ws-2#1', 'flow:root#1', 'workstream', 'Batch the handoff'),
  span('op:1', 'flow:root/ws-1#1', 'agent_turn', 'implementer'),
  span('op:2', 'flow:root/ws-2#1', 'agent_turn', 'implementer'),
  span('op:3', 'flow:root/ws-1#1', 'gate', 'build'),
];

const TURNS = ['op:1', 'op:2'] as const;

function say(sequence: number, spanId: string | null, content: string, channel = 'assistant') {
  return event(sequence, 'agent_output_chunk', {
    invocation_id: `inv-${spanId}`,
    agent_kind: 'implementer',
    ...(spanId === null ? {} : {span_id: spanId}),
    data: {kind: 'agent_output_chunk', channel: channel as 'assistant', content},
  });
}

function call(sequence: number, spanId: string, callId: string, command: string) {
  return event(sequence, 'tool_call', {
    invocation_id: `inv-${spanId}`,
    agent_kind: 'implementer',
    span_id: spanId,
    data: {kind: 'tool_call', tool: 'Bash', call_id: callId, args: {command}},
  });
}

function result(sequence: number, spanId: string, callId: string, content: string, error = false) {
  return event(sequence, 'tool_result', {
    invocation_id: `inv-${spanId}`,
    agent_kind: 'implementer',
    span_id: spanId,
    data: {kind: 'tool_result', tool: 'Bash', call_id: callId, content, is_error: error},
  });
}

/** A page that folded `transcript`, and the step whose turns the test reads. */
interface Selected {
  readonly state: RunPageState;
  readonly span: string;
}

function page(transcript: readonly RunEvent[], span: string): Selected {
  const actions: RunPageAction[] = [
    {type: 'run-core', core: reduceEventBatch(initialCoreState(), transcript)},
  ];
  let state = runPageInitial({
    identity: RUN,
    title: {project: '~/vibesys-projects/queue-rs', task: 'mpmc'},
    hostLabel: 'host-b',
    unavailable: {},
  });
  // The page folds run data only while attached to the live run.
  state = {...state, link: {kind: 'attached', instance: 'a1b2c3d4e5f6'}};
  state = runPageStep(state, {
    type: 'run-data',
    wallMs: 0,
    data: {kind: 'events', events: SPANS.map((each, index) => started(index + 1, each))},
  }).state;
  for (const action of actions) state = runPageStep(state, action).state;
  return {state, span};
}

function turns(selected: Selected): ReturnType<typeof turnTranscript> {
  const viewed = viewedRun(selected.state);
  return turnTranscript(viewed.trace, viewed.core, selected.span);
}

const kinds = (rows: readonly TurnRow[]): string[] => rows.map(row => row.kind);

describe('the turns under a step', () => {
  test('are unknown until the run’s fold has reached the page', () => {
    const {state, span} = page([], 'op:1');
    expect(turnTranscript(viewedRun(state).trace, null, span)).toBeNull();
  });

  test('a selected agent turn shows its messages, reasoning and tool calls in order', () => {
    const state = page(
      [
        say(10, 'op:1', 'Thinking it over. ', 'analysis'),
        say(11, 'op:1', 'I will pad the counters.'),
        call(12, 'op:1', 'c1', 'cargo bench'),
        result(13, 'op:1', 'c1', 'ok 14 benches'),
        call(14, 'op:1', 'c2', 'cargo test'),
      ],
      'op:1',
    );
    const view = turns(state);
    expect(view?.turns).toBe(1);
    expect(view?.rows).toEqual([
      {kind: 'heading', id: 'turn op:1', title: 'Builder', detail: 'Running'},
      expect.objectContaining({kind: 'message', variant: 'reasoning', text: 'Thinking it over. '}),
      expect.objectContaining({
        kind: 'message',
        variant: 'reply',
        author: 'Builder',
        text: 'I will pad the counters.',
      }),
      {
        kind: 'tool',
        id: '12',
        name: 'Bash',
        state: 'ok',
        summary: 'cargo bench',
        meta: '13 chars',
        input: '{\n  "command": "cargo bench"\n}',
        output: 'ok 14 benches',
        elapsed: 2,
      },
      expect.objectContaining({kind: 'tool', name: 'Bash', state: 'pending', output: null}),
    ]);
  });

  test('a failed tool result reads as failed, and a call that never got one stops running with its turn', () => {
    const ended = SPANS.map(each =>
      each.id === 'op:1' ? {...each, status: 'done' as const} : each,
    );
    let state = page(
      [call(10, 'op:1', 'c1', 'make'), result(11, 'op:1', 'c1', 'boom', true)],
      'op:1',
    );
    expect(turns(state)?.rows[1]).toMatchObject({kind: 'tool', state: 'failed', output: 'boom'});
    state = page([call(10, 'op:1', 'c1', 'make')], 'op:1');
    state = {
      ...state,
      state: runPageStep(state.state, {
        type: 'run-data',
        wallMs: 0,
        data: {
          kind: 'events',
          events: ended
            .filter(each => each.id === 'op:1')
            .map(each => ({
              ...event(20, 'span_ended'),
              data: {kind: 'span_ended', span: each},
            })) as RunEvent[],
        },
      }).state,
    };
    expect(turns(state)?.rows[1]).toMatchObject({kind: 'tool', state: 'ok', output: null});
  });

  test('a selected workstream shows its agents’ turns, one section each, in the order they started', () => {
    // The two workstreams’ agents ran side by side, so their events interleave.
    const state = page(
      [
        say(10, 'op:2', 'second agent, '),
        say(11, 'op:1', 'first agent, '),
        say(12, 'op:2', 'still talking'),
        say(13, 'op:1', 'done'),
      ],
      'flow:root/ws-1#1',
    );
    const view = turns(state);
    expect(view?.turns).toBe(1);
    expect(view?.rows.map(row => (row.kind === 'message' ? row.text : row.kind))).toEqual([
      'heading',
      'first agent, done',
    ]);

    const both = turns(page([say(10, 'op:2', 'b'), say(11, 'op:1', 'a')], 'flow:root#1'));
    expect(both?.turns).toBe(2);
    expect(both?.rows.map(row => (row.kind === 'message' ? row.text : row.id))).toEqual([
      'turn op:1',
      'a',
      'turn op:2',
      'b',
    ]);
  });

  test('a step with no agent turns shows none, and events with no span are in no step', () => {
    expect(turns(page([say(10, 'op:1', 'words')], 'op:3'))?.rows).toEqual([]);
    const untied = turns(page([say(10, null, 'no span'), say(11, 'op:1', 'words')], 'flow:root#1'));
    expect(untied?.rows.filter(row => row.kind === 'message').map(row => row.id)).toEqual(['11']);
  });

  test('selecting another step changes what is shown, with no other input', () => {
    const events = [say(10, 'op:1', 'first'), say(11, 'op:2', 'second')];
    const text = (selected: string): string[] =>
      (turns(page(events, selected))?.rows ?? []).flatMap(row =>
        row.kind === 'message' ? [row.text] : [],
      );
    expect(text('op:1')).toEqual(['first']);
    expect(text('op:2')).toEqual(['second']);
    expect(text('flow:root/ws-2#1')).toEqual(['second']);
  });

  test('every entry the server tied to a turn is shown once under the root, whatever the interleaving', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const events: RunEvent[] = [];
      const expected = new Map<string, number>();
      for (let sequence = 10; sequence < 10 + random.int(40) + 1; sequence += 1) {
        const turn = random.pick(TURNS);
        events.push(
          random.bool()
            ? say(sequence, turn, `${sequence} `)
            : call(sequence, turn, `c${sequence}`, `cmd ${sequence}`),
        );
        expected.set(turn, (expected.get(turn) ?? 0) + 1);
      }
      const rows = turns(page(events, 'flow:root#1'))?.rows ?? [];
      const shown = rows.filter(row => row.kind !== 'heading');
      expect(kinds(rows).filter(kind => kind === 'heading').length, `seed ${seed}`).toBe(
        expected.size,
      );
      // Chunks of one turn glue into one message, so count characters of text and calls.
      const words = shown.flatMap(row => (row.kind === 'message' ? row.text.split(' ') : []));
      const calls = shown.filter(row => row.kind === 'tool').length;
      const sent = events.filter(each => each.type === 'agent_output_chunk').length;
      expect(words.filter(word => word !== '').length, `seed ${seed}`).toBe(sent);
      expect(calls, `seed ${seed}`).toBe(events.length - sent);
    }
  });
});

/** What the server sends for a judge's turn: the reply model's schema and the validated reply. */
const JUDGE_SCHEMA = {
  $defs: {Verdict: {enum: ['pass', 'fail'], type: 'string'}},
  properties: {
    analysis: {type: 'string'},
    feedback: {type: 'string'},
    verdict: {$ref: '#/$defs/Verdict'},
    skills_used: {type: 'array', items: {type: 'object'}},
  },
  required: ['analysis', 'feedback', 'verdict'],
  type: 'object',
};

function replied(
  sequence: number,
  spanId: string,
  kind: string,
  schema: Record<string, unknown>,
  value: Record<string, unknown>,
): RunEvent {
  return event(sequence, 'agent_execution_finished', {
    invocation_id: `inv-${spanId}`,
    agent_kind: 'judge',
    span_id: spanId,
    status: 'completed',
    data: {
      kind: 'agent_execution_finished',
      result: JSON.stringify({reply: value}) as unknown as Record<string, unknown>,
      reply: {kind, json_schema: schema, value},
    },
  });
}

function rowsOf(events: readonly RunEvent[], selected: string): readonly TurnRow[] {
  return turns(page(events, selected))?.rows ?? [];
}

describe('a structured reply under a step', () => {
  const JUDGE = {
    analysis: 'The current src/lib.rs matches.',
    feedback: '',
    verdict: 'pass',
    skills_used: [],
  };

  test('a judge reply shows its analysis as text and its verdict as a badge, with no braces', () => {
    const raw = JSON.stringify({reply: JUDGE});
    const rows = rowsOf(
      [say(10, 'op:1', raw), replied(11, 'op:1', 'JudgeResponse', JUDGE_SCHEMA, JUDGE)],
      'op:1',
    );
    expect(rows.map(row => row.kind)).toEqual(['heading', 'reply']);
    const reply = rows[1];
    if (reply?.kind !== 'reply') throw new Error('no reply row');
    expect(reply.title).toBe('Reviewer response');
    expect(reply.fields).toEqual([
      {kind: 'prose', key: 'analysis', label: 'Analysis', text: 'The current src/lib.rs matches.'},
      {kind: 'badge', key: 'verdict', label: 'Verdict', value: 'pass', tone: 'ok'},
    ]);
    const shown = JSON.stringify(
      reply.fields.map(field => [field.label, 'text' in field ? field.text : '']),
    );
    expect(shown).not.toContain('{');
    expect(reply.raw).toContain('"verdict": "pass"');
  });

  test('a failing verdict reads as an error badge, and empty fields are not shown', () => {
    const rows = rowsOf(
      [
        replied(10, 'op:1', 'JudgeResponse', JUDGE_SCHEMA, {
          ...JUDGE,
          feedback: '',
          verdict: 'fail',
        }),
      ],
      'op:1',
    );
    const reply = rows.find(row => row.kind === 'reply');
    expect(reply?.kind === 'reply' && reply.fields.map(field => field.key)).toEqual([
      'analysis',
      'verdict',
    ]);
    expect(reply?.kind === 'reply' && reply.fields[1]).toMatchObject({tone: 'err'});
  });

  test('a reply of a kind nothing knows reads generically: text, lists, nested records', () => {
    const value = {
      summary: 'Padded the counters.',
      files_touched: ['src/queue.rs', 'src/lib.rs'],
      metrics: {ops: 41.2, noisy: false},
      notes: [
        {title: 'a', detail: 'first'},
        {title: 'b', detail: ''},
      ],
      empty: [],
      nothing: null,
    };
    const rows = rowsOf([replied(10, 'op:1', 'BrandNewReply', {}, value)], 'op:1');
    const reply = rows.find(row => row.kind === 'reply');
    if (reply?.kind !== 'reply') throw new Error('no reply row');
    expect(reply.title).toBe('Brand new reply');
    expect(reply.fields.map(field => [field.kind, field.key])).toEqual([
      ['prose', 'summary'],
      ['list', 'files_touched'],
      ['group', 'metrics'],
      ['group', 'notes'],
    ]);
  });
});

describe('a tool call in the turn view', () => {
  test('is one line of name, main argument and result size, with the full text behind it', () => {
    const output = 'a\nb\nc';
    const rows = rowsOf(
      [call(10, 'op:1', 'c1', 'cargo bench --bench queue'), result(11, 'op:1', 'c1', output)],
      'op:1',
    );
    expect(rows[1]).toMatchObject({
      kind: 'tool',
      name: 'Bash',
      summary: 'cargo bench --bench queue',
      meta: '3 lines',
      output,
      state: 'ok',
    });
  });

  test('says why a failed or empty result has no size', () => {
    const failed = rowsOf(
      [call(10, 'op:1', 'c1', 'x'), result(11, 'op:1', 'c1', 'boom', true)],
      'op:1',
    );
    expect(failed[1]).toMatchObject({state: 'failed', meta: 'failed · 4 chars'});
    const empty = rowsOf([call(10, 'op:1', 'c1', 'x'), result(11, 'op:1', 'c1', '')], 'op:1');
    expect(empty[1]).toMatchObject({meta: 'no output'});
    expect(rowsOf([call(10, 'op:1', 'c1', 'x')], 'op:1')[1]).not.toHaveProperty('meta');
  });
});

function commandResult(
  sequence: number,
  callId: string,
  command: {stdout: string; stderr: string; exit_code: number | null; duration: number | null},
) {
  return event(sequence, 'tool_result', {
    invocation_id: 'inv-op:1',
    agent_kind: 'implementer',
    span_id: 'op:1',
    data: {
      kind: 'tool_result',
      tool: 'Bash',
      call_id: callId,
      content: command.stdout || command.stderr,
      is_error: false,
      payload: {kind: 'command', ...command},
    },
  });
}

type ToolTurnRow = Extract<TurnRow, {kind: 'tool'}>;
const toolRows = (rows: readonly TurnRow[] | undefined): ToolTurnRow[] =>
  (rows ?? []).filter((row): row is ToolTurnRow => row.kind === 'tool');

describe('a command result in a turn’s rows', () => {
  test('keeps the exit code, the run time and both streams as the result reported them', () => {
    fc.assert(
      fc.property(
        fc.string(),
        fc.string(),
        fc.option(fc.integer({min: 0, max: 255}), {nil: null}),
        fc.option(fc.double({min: 0, max: 1e5, noNaN: true}), {nil: null}),
        (stdout, stderr, exit_code, duration) => {
          const state = page(
            [
              call(10, 'op:1', 'c1', 'x'),
              commandResult(11, 'c1', {stdout, stderr, exit_code, duration}),
            ],
            'op:1',
          );
          const [row] = toolRows(turns(state)?.rows);
          expect(row?.command).toEqual({exitCode: exit_code, seconds: duration, stdout, stderr});
        },
      ),
    );
  });

  test('says how it ended in words: the exit code, then the time, tenths below ten seconds', () => {
    const words = (exit_code: number | null, duration: number | null): string | undefined => {
      const state = page(
        [
          call(10, 'op:1', 'c1', 'x'),
          commandResult(11, 'c1', {stdout: 'out', stderr: '', exit_code, duration}),
        ],
        'op:1',
      );
      return toolRows(turns(state)?.rows)[0]?.meta;
    };
    expect(words(0, 2.34)).toBe('exit 0 · 2.3s');
    expect(words(1, 0.04)).toBe('exit 1 · 0.0s');
    expect(words(2, 9.94)).toBe('exit 2 · 9.9s');
    expect(words(2, 9.96)).toBe('exit 2 · 10s');
    expect(words(0, 59)).toBe('exit 0 · 59s');
    expect(words(0, 125)).toBe('exit 0 · 2m');
    expect(words(137, null)).toBe('exit 137');
    expect(words(null, 3)).toBe('3.0s');
  });

  test('falls back to the size of the output when it reported neither, and for any other result', () => {
    const none = page(
      [
        call(10, 'op:1', 'c1', 'x'),
        commandResult(11, 'c1', {stdout: 'built', stderr: '', exit_code: null, duration: null}),
      ],
      'op:1',
    );
    expect(toolRows(turns(none)?.rows)[0]?.meta).toBe('5 chars');
    const plain = page([call(10, 'op:1', 'c1', 'x'), result(11, 'op:1', 'c1', 'a\nb')], 'op:1');
    const [row] = toolRows(turns(plain)?.rows);
    expect(row?.meta).toBe('2 lines');
    expect(row?.command).toBeUndefined();
  });

  test('a call that has no result yet has none of it', () => {
    const [row] = toolRows(turns(page([call(10, 'op:1', 'c1', 'x')], 'op:1'))?.rows);
    expect(row?.state).toBe('pending');
    expect(row?.command).toBeUndefined();
    expect(row?.meta).toBeUndefined();
  });
});

describe('when in a turn a row happened', () => {
  test('is the seconds since the turn’s first timed entry, never negative or backwards', () => {
    fc.assert(
      fc.property(fc.array(fc.integer({min: 0, max: 600}), {minLength: 1, maxLength: 25}), gaps => {
        const events: RunEvent[] = [];
        let at = 0;
        gaps.forEach((gap, index) => {
          at += gap;
          events.push({
            ...call(10 + index, 'op:1', `c${index}`, `cmd ${index}`),
            timestamp: new Date(Date.parse('2026-10-15T16:00:00Z') + at * 1000).toISOString(),
          });
        });
        const elapsed = toolRows(turns(page(events, 'op:1'))?.rows).map(row => row.elapsed);
        const offsets = gaps.map((_, index) => gaps.slice(0, index + 1).reduce((a, b) => a + b, 0));
        expect(elapsed).toEqual(offsets.map(each => each - (offsets[0] ?? 0)));
        expect(elapsed.every(each => each !== undefined && each >= 0)).toBe(true);
      }),
    );
  });

  test('counts from the first entry that has a time, and is absent when no entry has one', () => {
    const untimed = (sequence: number) =>
      ({...say(sequence, 'op:1', 'hello '), timestamp: 'unknown'}) as RunEvent;
    const mixed = page(
      [untimed(10), call(11, 'op:1', 'c1', 'a'), call(14, 'op:1', 'c2', 'b')],
      'op:1',
    );
    expect(toolRows(turns(mixed)?.rows).map(row => row.elapsed)).toEqual([0, 3]);
    const none = page(
      [
        {...call(11, 'op:1', 'c1', 'a'), timestamp: 'unknown'} as RunEvent,
        {...call(12, 'op:1', 'c2', 'b'), timestamp: 'unknown'} as RunEvent,
      ],
      'op:1',
    );
    expect(toolRows(turns(none)?.rows).every(row => !('elapsed' in row))).toBe(true);
  });

  test('is counted from each turn’s own start under a workstream', () => {
    const state = page(
      [
        call(10, 'op:1', 'a1', 'first'),
        call(12, 'op:2', 'b1', 'second'),
        call(14, 'op:1', 'a2', 'third'),
        call(17, 'op:2', 'b2', 'fourth'),
      ],
      'flow:root#1',
    );
    expect(toolRows(turns(state)?.rows).map(row => `${row.summary} ${row.elapsed}`)).toEqual([
      'first 0',
      'third 4',
      'second 0',
      'fourth 5',
    ]);
  });
});
