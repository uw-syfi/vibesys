/** The time a transcript entry records, on the event path: the time of the event that began it. */
import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from './index.js';
import {transcriptBySpan} from './transcript/index.js';

const ENVELOPE = {span_id: 'turn', invocation_id: 'inv', agent_kind: 'worker'} as const;
const START = Date.parse('2026-10-15T16:00:00Z');

const clock = (offsetMs: number): string => new Date(START + offsetMs).toISOString();

function say(
  sequence: number,
  offsetMs: number,
  content: string,
  channel: 'assistant' | 'analysis',
) {
  return event(sequence, 'agent_output_chunk', {
    ...ENVELOPE,
    timestamp: clock(offsetMs),
    data: {kind: 'agent_output_chunk', channel, content},
  });
}

function call(sequence: number, offsetMs: number, callId: string) {
  return event(sequence, 'tool_call', {
    ...ENVELOPE,
    timestamp: clock(offsetMs),
    data: {kind: 'tool_call', tool: 'Bash', call_id: callId, args: {command: 'ls'}},
  });
}

function result(sequence: number, offsetMs: number, callId: string) {
  return event(sequence, 'tool_result', {
    ...ENVELOPE,
    timestamp: clock(offsetMs),
    data: {kind: 'tool_result', tool: 'Bash', call_id: callId, content: 'a', is_error: false},
  });
}

function entriesOf(events: readonly RunEvent[]) {
  const core = reduceEventBatch(initialCoreState(), events);
  return transcriptBySpan(core.transcript, new Set(['turn'])).get('turn') ?? [];
}

/** A turn of chunks and tool calls at increasing times, the same for a seed; and those times. */
function randomTurn(seed: number): {events: RunEvent[]; times: Set<number>} {
  let state = seed;
  const next = (n: number): number => {
    state = (Math.imul(state, 1_103_515_245) + 12_345) >>> 0;
    return state % n;
  };
  const events: RunEvent[] = [];
  const times = new Set<number>();
  const open: string[] = [];
  let offset = 0;
  for (let sequence = 1; sequence <= 30; sequence += 1) {
    offset += next(5_000);
    times.add(START + offset);
    const choice = next(4);
    const closing = choice === 0 ? open.splice(next(open.length), 1)[0] : undefined;
    if (closing !== undefined) {
      events.push(result(sequence, offset, closing));
    } else if (choice === 1) {
      open.push(`c${sequence}`);
      events.push(call(sequence, offset, `c${sequence}`));
    } else {
      events.push(say(sequence, offset, `w${sequence} `, choice === 2 ? 'analysis' : 'assistant'));
    }
  }
  return {events, times};
}

describe('the time of a transcript entry', () => {
  it('is the time of its event, in epoch milliseconds', () => {
    const [entry] = entriesOf([say(1, 2_500, 'hello', 'assistant')]);
    expect(entry?.at).toBe(START + 2_500);
  });

  it('stays the first chunk’s when later chunks of the turn glue onto it', () => {
    const entries = entriesOf([
      say(1, 1_000, 'The head ', 'analysis'),
      say(2, 4_000, 'and tail ', 'analysis'),
      say(3, 9_000, 'share a line.', 'analysis'),
    ]);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.content).toBe('The head and tail share a line.');
    expect(entries[0]?.at).toBe(START + 1_000);
  });

  it('stays the call’s when its result joins it', () => {
    const entries = entriesOf([call(1, 1_000, 'c1'), result(2, 6_000, 'c1')]);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.toolResult).toBeDefined();
    expect(entries[0]?.at).toBe(START + 1_000);
  });

  it('is absent, and harmless, when the event’s time cannot be read', () => {
    const odd = {...say(1, 0, 'x', 'assistant'), timestamp: 'garbled'} as RunEvent;
    const [entry] = entriesOf([odd]);
    expect(entry?.content).toBe('x');
    expect('at' in (entry ?? {})).toBe(false);
  });

  it('never runs backwards along a turn, and is always one of its events’ times', () => {
    for (let seed = 1; seed <= 40; seed += 1) {
      const {events, times} = randomTurn(seed);
      const ats = entriesOf(events).map(entry => entry.at);
      expect(ats.every(at => at !== undefined && times.has(at))).toBe(true);
      expect([...ats]).toEqual([...ats].sort((a, b) => (a ?? 0) - (b ?? 0)));
    }
  });
});
