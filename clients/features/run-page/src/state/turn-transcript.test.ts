import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type RunPageAction,
  type RunPageState,
  runPageInitial,
  runPageStep,
  type TurnRow,
  turnTranscript,
  viewedRun,
} from './index.js';

/** A run's turns as the server sends them: span events, then each turn's events stamped with it. */

const RUN = {host: 'ssh:chelan4', runId: '20261015-163047'} as const;

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

function page(transcript: readonly RunEvent[], selected: string | null): RunPageState {
  const actions: RunPageAction[] = [
    {type: 'run-core', core: reduceEventBatch(initialCoreState(), transcript)},
    ...(selected === null ? [] : [{type: 'map-select' as const, id: selected}]),
  ];
  let state = runPageInitial({
    identity: RUN,
    title: {project: '~/vibesys-projects/queue-rs', task: 'mpmc'},
    hostLabel: 'chelan4',
    unavailable: {},
  });
  // The page folds run data only while attached to the live run.
  state = {...state, link: {kind: 'attached', instance: 'a1b2c3d4e5f6'}};
  state = runPageStep(state, {
    type: 'run-data',
    data: {kind: 'events', events: SPANS.map((each, index) => started(index + 1, each))},
  }).state;
  for (const action of actions) state = runPageStep(state, action).state;
  return state;
}

function turns(state: RunPageState): ReturnType<typeof turnTranscript> {
  const viewed = viewedRun(state);
  return turnTranscript(viewed.trace, viewed.core, state.map);
}

const kinds = (rows: readonly TurnRow[]): string[] => rows.map(row => row.kind);

describe('the turns under the selected Map step', () => {
  test('are unknown until the run’s fold has reached the page', () => {
    const state = page([], 'op:1');
    expect(turnTranscript(viewedRun(state).trace, null, state.map)).toBeNull();
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
      {kind: 'heading', id: 'turn op:1', title: 'implementer', detail: 'Running'},
      expect.objectContaining({kind: 'message', variant: 'reasoning', text: 'Thinking it over. '}),
      expect.objectContaining({
        kind: 'message',
        variant: 'reply',
        author: 'Implementer',
        text: 'I will pad the counters.',
      }),
      {
        kind: 'tool',
        id: '12',
        name: 'Bash',
        state: 'ok',
        summary: 'cargo bench',
        input: '{\n  "command": "cargo bench"\n}',
        output: 'ok 14 benches',
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
    state = runPageStep(state, {
      type: 'run-data',
      data: {
        kind: 'events',
        events: ended
          .filter(each => each.id === 'op:1')
          .map(each => ({
            ...event(20, 'span_ended'),
            data: {kind: 'span_ended', span: each},
          })) as RunEvent[],
      },
    }).state;
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
