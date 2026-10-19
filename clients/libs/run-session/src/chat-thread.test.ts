import {expect, test} from 'bun:test';
import type {ProtocolResponse} from '@vibesys/contracts';
import {ServerError} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {FakeLifecycle, sessionWith} from './testing/index.js';

const base: ProtocolResponse = {
  protocol_version: 1,
  request_id: 'thread',
  timestamp: '2026-01-01T00:00:00Z',
  ok: true,
};
const thread = {thread_id: 'thread-b', title: '', provider: 'codex', model: 'gpt-x'};

test('creating a thread sends the chosen harness, folds its creation event, and returns the thread', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  await session.start();
  const made = event(2, 'chat_thread_created', {
    run_id: 'run-1',
    agent_kind: 'chat',
    chat_thread_id: 'thread-b',
    data: {
      kind: 'chat_thread_created',
      thread_id: 'thread-b',
      title: '',
      provider: 'codex',
      model: 'gpt-x',
      created_at: '2026-01-01T00:00:00Z',
    },
  });
  transport.snapshots.push({...base, chat_thread: thread, events: [made]});
  await expect(session.createChatThread({provider: 'codex', model: 'gpt-x'})).resolves.toEqual(
    thread,
  );
  expect(transport.requests.at(-1)).toEqual({
    type: 'query.chat_thread_create',
    harness: {provider: 'codex', model: 'gpt-x'},
  });
  expect(session.store.getState().chatThreads.map(each => each.id)).toContain('thread-b');
  transport.snapshots.push({...base, chat_thread: thread, events: []});
  await session.createChatThread(null);
  expect(transport.requests.at(-1)).toEqual({type: 'query.chat_thread_create'});
  await session.close();
});

test('a thread the server did not create is a typed server error', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  await session.start();
  transport.snapshots.push({...base, ok: false, error: 'no such model'});
  await expect(session.createChatThread(null)).rejects.toBeInstanceOf(ServerError);
  await session.close();
});
