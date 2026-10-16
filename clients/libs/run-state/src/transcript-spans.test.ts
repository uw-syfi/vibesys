import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from './index.js';
import {transcriptBySpan} from './transcript/index.js';

/** A seeded generator, so a failing seed is pinned and replayed exactly (mulberry32). */
function seeded(seed: number): (n: number) => number {
  let state = seed >>> 0 || 1;
  return n => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return Math.floor((((t ^ (t >>> 14)) >>> 0) / 4294967296) * n);
  };
}

type Body =
  | {kind: 'say'; channel: 'assistant' | 'analysis'; content: string}
  | {kind: 'call'; callId: string; tool: string}
  | {kind: 'result'; callId: string; tool: string; content: string};

/** One span's turn: a few chunks and tool calls, each result after its call. */
function turn(random: (n: number) => number, span: string): Body[] {
  const bodies: Body[] = [];
  const open: string[] = [];
  for (let step = random(10) + 3; step > 0; step -= 1) {
    const choice = random(4);
    if (choice === 0 && open.length > 0) {
      const callId = open.splice(random(open.length), 1)[0] as string;
      bodies.push({kind: 'result', callId, tool: 'Read', content: `result ${callId}`});
    } else if (choice === 1) {
      const callId = `${span}-c${bodies.length}`;
      open.push(callId);
      bodies.push({kind: 'call', callId, tool: 'Read'});
    } else {
      const channel = choice === 2 ? 'assistant' : 'analysis';
      bodies.push({kind: 'say', channel, content: `${span} words ${bodies.length} `});
    }
  }
  return bodies;
}

function toEvent(sequence: number, span: string, body: Body): RunEvent {
  const envelope = {span_id: span, invocation_id: `inv-${span}`, agent_kind: 'worker'};
  if (body.kind === 'say') {
    const data = {
      kind: 'agent_output_chunk' as const,
      channel: body.channel,
      content: body.content,
    };
    return event(sequence, 'agent_output_chunk', {...envelope, data});
  }
  if (body.kind === 'call') {
    const data = {kind: 'tool_call' as const, tool: body.tool, call_id: body.callId, args: {}};
    return event(sequence, 'tool_call', {...envelope, data});
  }
  const data = {
    kind: 'tool_result' as const,
    tool: body.tool,
    call_id: body.callId,
    content: body.content,
    is_error: false,
  };
  return event(sequence, 'tool_result', {...envelope, data});
}

/** The turns of `spans`, their events interleaved in an order the seed chose. */
function interleaved(
  seed: number,
  spans: readonly string[],
): {all: RunEvent[]; each: RunEvent[][]} {
  const random = seeded(seed);
  const queues = spans.map(span => turn(random, span).map(body => ({span, body})));
  const pending = queues.map(queue => [...queue]);
  const all: RunEvent[] = [];
  const each: RunEvent[][] = spans.map(() => []);
  for (let sequence = 1; pending.some(queue => queue.length > 0); sequence += 1) {
    const live = pending.flatMap((queue, index) => (queue.length > 0 ? [index] : []));
    const index = live[random(live.length)] as number;
    const next = pending[index]?.shift();
    if (next === undefined) throw new Error('a live queue is empty');
    const built = toEvent(sequence, next.span, next.body);
    all.push(built);
    each[index]?.push(built);
  }
  return {all, each};
}

describe('a span read out of the run transcript', () => {
  const SEEDS = Array.from({length: 300}, (_, index) => index + 1);

  it('is what folding that span’s events alone gives, whatever ran beside it', () => {
    let fragmented = 0;
    for (const seed of SEEDS) {
      const spans = ['flow:a', 'flow:b', 'flow:c'].slice(0, (seed % 3) + 1);
      const {all, each} = interleaved(seed, spans);
      const whole = reduceEventBatch(initialCoreState(), all).transcript;
      let aloneEntries = 0;
      for (const [index, span] of spans.entries()) {
        const alone = reduceEventBatch(initialCoreState(), each[index] ?? []).transcript;
        aloneEntries += alone.length;
        const read = transcriptBySpan(whole, new Set(spans)).get(span) ?? [];
        expect(read, `seed ${seed}, ${span}`).toEqual(alone);
      }
      if (whole.length > aloneEntries) fragmented += 1;
    }
    // The property is not vacuous: running side by side did split turns in the whole transcript.
    expect(fragmented).toBeGreaterThan(0);
  });

  it('holds only entries of the asked spans, in the order the run had them', () => {
    for (const seed of SEEDS) {
      const spans = ['flow:a', 'flow:b', 'flow:c'];
      const whole = reduceEventBatch(initialCoreState(), interleaved(seed, spans).all).transcript;
      const asked = new Set(spans.filter((_, index) => (seed >> index) % 2 === 0));
      const read = transcriptBySpan(whole, asked);
      expect([...read.keys()].every(span => asked.has(span))).toBe(true);
      for (const [span, entries] of read) {
        expect(entries.every(entry => entry.spanId === span)).toBe(true);
        const positions = entries.map(entry =>
          whole.findIndex(candidate => candidate.id === entry.id),
        );
        expect(positions, `seed ${seed}, ${span}`).toEqual([...positions].sort((a, b) => a - b));
      }
    }
  });

  it('never takes an entry the server did not tie to a span', () => {
    const untied = event(1, 'agent_output_chunk', {
      invocation_id: 'inv-x',
      data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'no span'},
    });
    const tied = toEvent(2, 'flow:a', {kind: 'say', channel: 'assistant', content: 'words'});
    const whole = reduceEventBatch(initialCoreState(), [untied, tied]).transcript;
    expect(whole.map(entry => entry.spanId)).toEqual([undefined, 'flow:a']);
    expect([...transcriptBySpan(whole, new Set(['flow:a']))]).toEqual([
      ['flow:a', [expect.objectContaining({content: 'words'})]],
    ]);
    expect(transcriptBySpan(whole, new Set()).size).toBe(0);
  });
});
