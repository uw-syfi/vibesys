import {expect, test} from 'bun:test';
import type {ProtocolResponse} from '@vibesys/contracts';
import {ServerError} from '@vibesys/contracts';
import {event, eventBatch} from '@vibesys/contracts/testing';
import {deferred, FakeLifecycle, sessionWith} from './testing/index.js';

const reply: ProtocolResponse = {
  protocol_version: 1,
  request_id: 'chat',
  timestamp: '2026-01-01T00:00:00Z',
  ok: true,
  chat: {question: 'what changed?', answer: 'the answer', thread_id: 'thread-a'},
};

test('Ask preserves thread and cancellation, folds response events once before replay, and preserves the stream cursor', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  await session.start();
  const answerEvent = event(2, 'chat', {
    run_id: 'run-1',
    text: 'what changed?',
    agent_kind: 'chat',
    chat_thread_id: 'thread-a',
    data: {kind: 'chat', answer: 'the answer'},
  });
  transport.snapshots.push({...reply, events: [answerEvent]});
  const controller = new AbortController();
  await expect(
    session.ask('what changed?', {threadId: 'thread-a', signal: controller.signal}),
  ).resolves.toEqual({question: 'what changed?', answer: 'the answer', thread_id: 'thread-a'});
  expect(transport.requests.at(-1)).toEqual({
    type: 'query.chat',
    text: 'what changed?',
    thread_id: 'thread-a',
  });
  expect(transport.requestOptions.at(-1)?.signal).toBe(controller.signal);
  expect(session.store.getState().sequence).toBe(1);
  expect(session.store.getState().chatTranscripts['thread-a']?.map(entry => entry.content)).toEqual(
    ['the answer'],
  );
  transport.subscriptions[0]?.onMessage(
    eventBatch([answerEvent], {
      through_sequence: 2,
      store_id: 'store-1',
      history_after_sequence: 0,
    }),
  );
  expect(session.store.getState().sequence).toBe(2);
  expect(session.store.getState().chatTranscripts['thread-a']?.map(entry => entry.content)).toEqual(
    ['the answer'],
  );
  await session.close();
});

test('missing and rejected answers preserve typed server errors without folding response events', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  await session.start();
  const before = session.store.getState();
  const missing = {...reply};
  delete missing.chat;
  for (const response of [{...reply, chat: null}, missing]) {
    transport.snapshots.push({...response, error: 'no answer'});
    await expect(session.ask('why?')).rejects.toBeInstanceOf(ServerError);
    expect(session.store.getState()).toBe(before);
  }
  transport.snapshots.push({...reply, ok: false, error: 'rejected'});
  await expect(session.ask('why?')).rejects.toThrow('rejected');
  expect(session.store.getState()).toBe(before);
  await session.close();
});

test('a cancelled or closed session never folds a late chat response', async () => {
  for (const boundary of ['cancel', 'close'] as const) {
    const {session, transport} = sessionWith(new FakeLifecycle());
    await session.start();
    const controller = new AbortController();
    const late = deferred<ProtocolResponse>();
    transport.snapshots.push(late.promise);
    const request = session.ask('why?', {signal: controller.signal});
    const before = session.store.getState();
    if (boundary === 'cancel') controller.abort();
    else await session.close();
    late.resolve({
      ...reply,
      events: [event(2, 'chat', {agent_kind: 'chat', data: {kind: 'chat', answer: 'late'}})],
    });
    await request;
    expect(session.store.getState()).toBe(before);
    await session.close();
  }
});
