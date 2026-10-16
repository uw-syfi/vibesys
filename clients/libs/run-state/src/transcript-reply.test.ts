import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from './index.js';
import {transcriptBySpan} from './transcript/index.js';

const VALUE = {analysis: 'The edit is sound.', feedback: '', verdict: 'pass', skills_used: []};
const RAW = JSON.stringify({reply: VALUE});

/**
 * The generated type of `result` is an object (the Python field is `Any`, which the generator
 * renders as an open object), while the server sends the turn's raw text there.
 */
function text(value: string): Record<string, unknown> {
  return value as unknown as Record<string, unknown>;
}

function say(sequence: number, content: string, span = 'op:1', turn = 'inv-1'): RunEvent {
  return event(sequence, 'agent_output_chunk', {
    invocation_id: turn,
    agent_kind: 'judge',
    span_id: span,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  });
}

function finished(sequence: number, span = 'op:1', turn = 'inv-1', result = RAW): RunEvent {
  return event(sequence, 'agent_execution_finished', {
    invocation_id: turn,
    agent_kind: 'judge',
    span_id: span,
    status: 'completed',
    data: {
      kind: 'agent_execution_finished',
      result: text(result),
      reply: {kind: 'JudgeResponse', json_schema: {type: 'object'}, value: VALUE},
    },
  });
}

describe('a turn’s typed reply in the transcript', () => {
  it('takes the place of the raw JSON the turn streamed, whatever the chunking', () => {
    for (let cut = 1; cut < RAW.length; cut += 7) {
      const events = [say(1, RAW.slice(0, cut)), say(2, RAW.slice(cut)), finished(3)];
      const {transcript} = reduceEventBatch(initialCoreState(), events);
      expect(
        transcript.map(entry => entry.kind),
        `cut ${cut}`,
      ).toEqual(['reply']);
      expect(JSON.stringify(transcript[0]?.reply?.value)).toBe(JSON.stringify(VALUE));
      expect(transcript[0]?.spanId).toBe('op:1');
    }
  });

  it('keeps the words the agent wrote before the JSON, and the reply after them', () => {
    const events = [say(1, `I checked the edit.\n${RAW}`), finished(2)];
    const {transcript} = reduceEventBatch(initialCoreState(), events);
    expect(transcript.map(entry => [entry.kind, entry.content])).toEqual([
      ['assistant', 'I checked the edit.'],
      ['reply', RAW],
    ]);
  });

  it('folds a turn’s reply over its own text when other turns ran between its chunks', () => {
    const events = [
      say(1, RAW.slice(0, 10), 'op:1', 'inv-1'),
      say(2, 'other turn words', 'op:2', 'inv-2'),
      say(3, RAW.slice(10), 'op:1', 'inv-1'),
      finished(4),
    ];
    const {transcript} = reduceEventBatch(initialCoreState(), events);
    const turn = transcriptBySpan(transcript, new Set(['op:1'])).get('op:1') ?? [];
    expect(turn.map(entry => entry.kind)).toEqual(['reply']);
    expect(transcriptBySpan(transcript, new Set(['op:2'])).get('op:2')?.[0]?.content).toBe(
      'other turn words',
    );
  });

  it('leaves the text alone when it is not the reply’s, and still shows the reply', () => {
    const events = [say(1, 'different words'), finished(2)];
    const {transcript} = reduceEventBatch(initialCoreState(), events);
    expect(transcript.map(entry => entry.kind)).toEqual(['assistant', 'reply']);
  });

  it('a finish without a typed reply adds nothing', () => {
    const plain = event(1, 'agent_execution_finished', {
      invocation_id: 'inv-1',
      status: 'completed',
      data: {kind: 'agent_execution_finished', result: text('plain words')},
    });
    expect(reduceEventBatch(initialCoreState(), [plain]).transcript).toEqual([]);
  });
});
