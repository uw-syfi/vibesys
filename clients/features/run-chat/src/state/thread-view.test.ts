import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {type CoreState, initialCoreState, reduceEvent, reduceSnapshot} from '@vibesys/run-state';
import {shownThread, threadEntries, threadItems} from './thread-view.js';

const CHAT = {agent_kind: 'chat', round_label: 'experiment-chat'} as const;

function created(sequence: number, threadId: string, title = ''): RunEvent {
  return event(sequence, 'chat_thread_created', {
    ...CHAT,
    chat_thread_id: threadId,
    data: {
      kind: 'chat_thread_created',
      thread_id: threadId,
      title,
      provider: 'claude',
      model: 'opus',
      created_at: '2026-01-01T00:00:00Z',
    },
  });
}

function chunk(sequence: number, threadId: string, invocation: string, content: string): RunEvent {
  return event(sequence, 'agent_output_chunk', {
    ...CHAT,
    chat_thread_id: threadId,
    invocation_id: invocation,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  });
}

function answered(
  sequence: number,
  threadId: string,
  question: string,
  answer: string,
  invocation: string,
): RunEvent {
  return event(sequence, 'chat', {
    ...CHAT,
    chat_thread_id: threadId,
    text: question,
    data: {kind: 'chat', answer, invocation_id: invocation},
  });
}

function proposed(sequence: number, threadId: string, id: string): RunEvent {
  return event(sequence, 'steer_proposed', {
    chat_thread_id: threadId,
    data: {
      kind: 'steer_proposed',
      proposal_id: id,
      proposal: {text: 'keep bf16', target: {kind: 'role', id: 'implementer'}, mode: 'standing'},
    },
  });
}

function fold(events: readonly RunEvent[]): CoreState {
  return events.reduce(reduceEvent, initialCoreState());
}

describe('threadItems', () => {
  test('questions, streamed answers and proposals read in order', () => {
    const core = fold([
      created(1, 't'),
      chunk(2, 't', 'turn-1', 'The best '),
      chunk(3, 't', 'turn-1', 'idea is 4.'),
      proposed(4, 't', 'p-1'),
      answered(5, 't', 'what is best?', 'The best idea is 4.', 'turn-1'),
      chunk(6, 't', 'turn-2', 'Because'),
    ]);
    const items = threadItems(core, 't', 'why?');
    expect(items.map(item => item.kind)).toEqual([
      'question',
      'answer',
      'proposal',
      'question',
      'answer',
    ]);
    expect(items[0]).toMatchObject({text: 'what is best?'});
    expect(items[1]).toMatchObject({text: 'The best idea is 4.', streaming: false});
    // The question being answered shows, over the answer that has begun streaming.
    expect(items[3]).toMatchObject({text: 'why?'});
    expect(items[4]).toMatchObject({text: 'Because', streaming: true});
  });

  test('a question nothing has answered yet shows with a waiting marker', () => {
    const core = fold([created(1, 't')]);
    expect(threadItems(core, 't', 'hello').map(item => item.kind)).toEqual(['question', 'waiting']);
    expect(threadItems(core, 't', null)).toEqual([]);
  });

  test('a pending question is not shown twice once the fold has it', () => {
    const core = fold([chunk(1, 't', 'a', 'x'), answered(2, 't', 'q', 'x', 'a')]);
    const items = threadItems(core, 't', 'q');
    expect(items.filter(item => item.kind === 'question')).toHaveLength(2);
    // The caller clears its pending text when the ask ends; with it cleared there is one.
    expect(threadItems(core, 't', null).filter(item => item.kind === 'question')).toHaveLength(1);
  });

  test('no two items of a thread share an id, whether or not an answer streamed first', () => {
    // Each turn is a streamed or a bare answer; every mix of three turns, with a proposal after.
    for (let mix = 0; mix < 2 ** 3; mix++) {
      const events: RunEvent[] = [created(1, 't')];
      let sequence = 2;
      for (let turn = 0; turn < 3; turn++) {
        if ((mix >> turn) & 1) events.push(chunk(sequence++, 't', `turn-${turn}`, 'streamed'));
        events.push(answered(sequence++, 't', `question ${turn}`, 'streamed', `turn-${turn}`));
      }
      events.push(proposed(sequence, 't', 'p-1'));
      const ids = threadItems(fold(events), 't', 'next').map(item =>
        item.kind === 'proposal' ? item.proposal.id : item.id,
      );
      expect(new Set(ids).size, `mix ${mix}: ${ids.join(', ')}`).toBe(ids.length);
    }
  });

  test('a proposal alone, with no answers, still shows', () => {
    const items = threadItems(fold([created(1, 't'), proposed(2, 't', 'p-1')]), 't', null);
    expect(items.map(item => item.kind)).toEqual(['proposal']);
  });

  test('another thread never shows these items', () => {
    const core = fold([created(1, 't'), created(2, 'u'), proposed(3, 't', 'p-1')]);
    expect(threadItems(core, 'u', null)).toEqual([]);
  });
});

describe('threads', () => {
  const core = fold([created(1, 'a', 'First'), created(2, 'b'), created(3, 'c', 'Third')]);

  test('listed newest first, with a placeholder title for one with none', () => {
    expect(threadEntries(core).map(entry => [entry.id, entry.title])).toEqual([
      ['c', 'Third'],
      ['b', 'Untitled'],
      ['a', 'First'],
    ]);
    expect(threadEntries(core)[0]).toMatchObject({provider: 'claude', model: 'opus'});
  });

  test('a thread with no title is never listed under the name of the New chat action', () => {
    const untitled = fold([created(1, 'a'), created(2, 'b', 'Second'), created(3, 'c')]);
    const titles = threadEntries(untitled).map(entry => entry.title);
    expect(titles).toHaveLength(3);
    expect(titles).not.toContain('New chat');
  });

  test('the last thread opens by default, a chosen one wins, a new chat shows none', () => {
    const snapshot = reduceSnapshot(core, {
      run_id: 'run',
      status: 'running',
      sequence: 3,
      last_chat_thread_id: 'a',
    });
    expect(shownThread(snapshot, {kind: 'last'})).toBe('a');
    expect(shownThread(snapshot, {kind: 'thread', id: 'b'})).toBe('b');
    expect(shownThread(snapshot, {kind: 'new'})).toBeNull();
    // With no thread named by the server the newest is the last.
    expect(shownThread(core, {kind: 'last'})).toBe('c');
    expect(shownThread(initialCoreState(), {kind: 'last'})).toBeNull();
    expect(shownThread(core, {kind: 'thread', id: 'gone'})).toBeNull();
  });
});
