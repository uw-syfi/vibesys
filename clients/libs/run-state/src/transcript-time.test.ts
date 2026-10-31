/** The time a transcript entry records, on the event path: the time of the event that began it. */
import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import * as fc from 'fast-check';
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

/** One thing a turn did, and when (any time: events may be stamped out of the order they arrive). */
type Op = {readonly kind: 'analysis' | 'assistant' | 'call' | 'result'; readonly at: number};

const ops = fc.array(
  fc.record({
    kind: fc.constantFrom('analysis', 'assistant', 'call', 'result'),
    at: fc.integer({min: 0, max: 3_600_000}),
  }),
  {maxLength: 40},
);

/** The turn as events: a result closes the oldest open call, and with none open it is text. */
function turnOf(sequence: readonly Op[]): {events: RunEvent[]; asked: Op['kind'][]} {
  const events: RunEvent[] = [];
  const asked: Op['kind'][] = [];
  const open: string[] = [];
  sequence.forEach((op, index) => {
    const number = index + 1;
    const closing = op.kind === 'result' ? open.shift() : undefined;
    if (closing !== undefined) {
      events.push(result(number, op.at, closing));
      asked.push('result');
    } else if (op.kind === 'call') {
      open.push(`c${number}`);
      events.push(call(number, op.at, `c${number}`));
      asked.push('call');
    } else {
      const channel = op.kind === 'analysis' ? 'analysis' : 'assistant';
      events.push(say(number, op.at, `w${number} `, channel));
      asked.push(channel);
    }
  });
  return {events, asked};
}

/** The time each entry should carry, by the fold's rules: a chunk glues onto a last entry of its own kind, a result joins its call. */
function expectedTimes(sequence: readonly Op[], asked: readonly Op['kind'][]): number[] {
  const times: number[] = [];
  let last: Op['kind'] | undefined;
  sequence.forEach((op, index) => {
    const kind = asked[index];
    if (kind === 'result') return;
    if (kind === 'call') {
      times.push(START + op.at);
      last = 'call';
    } else if (kind !== last) {
      times.push(START + op.at);
      last = kind;
    }
  });
  return times;
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

  it('is absent when the event holds only the epoch, which is how a time never set reads', () => {
    const unset = {...say(1, 0, 'x', 'assistant'), timestamp: '1970-01-01T00:00:00Z'} as RunEvent;
    const [entry] = entriesOf([unset]);
    expect(entry?.content).toBe('x');
    expect('at' in (entry ?? {})).toBe(false);
  });

  it('is absent, and harmless, when the event’s time cannot be read', () => {
    const odd = {...say(1, 0, 'x', 'assistant'), timestamp: 'garbled'} as RunEvent;
    const [entry] = entriesOf([odd]);
    expect(entry?.content).toBe('x');
    expect('at' in (entry ?? {})).toBe(false);
  });

  it('is, for every entry of any turn, the time of the event that began it, however the times are ordered', () => {
    fc.assert(
      fc.property(ops, sequence => {
        const {events, asked} = turnOf(sequence);
        expect(entriesOf(events).map(entry => entry.at)).toEqual(expectedTimes(sequence, asked));
      }),
    );
  });
});
