import {expect, test} from 'bun:test';
import type {ProtocolResponse} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {deferred, FakeLifecycle, sessionWith} from '@vibesys/run-session/testing';
import {createConversationStore} from './index.js';

test('composed Ask folds authoritative events once and Steer reports its acknowledgement', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  await session.start();
  const store = createConversationStore(session, {connected: true, steerAvailable: true});
  const response: ProtocolResponse = {
    protocol_version: 1,
    timestamp: '2026-01-01T00:00:00Z',
    request_id: 'chat',
    ok: true,
    chat: {question: 'why?', answer: 'because'},
    events: [
      {
        ...event(2, 'chat', {text: 'why?'}),
        agent_kind: 'chat',
        data: {kind: 'chat', answer: 'because'},
      },
    ],
  };
  transport.snapshots.push(response);
  store.dispatch({type: 'draft', text: ' why? '});
  const askDone = until(store, () => store.getState().submissions[0]?.status === 'done');
  store.dispatch({type: 'submit', transcriptStart: 0});
  store.dispatch({type: 'submit', transcriptStart: 0});
  await askDone;
  expect(transport.requests.at(-1)).toEqual({type: 'query.chat', text: 'why?'});
  expect(store.getState().submissions).toMatchObject([{status: 'done', detail: null}]);
  expect(session.store.getState().chatTranscript.map(item => item.content)).toEqual(['because']);
  expect(session.store.getState().sequence).toBe(1);
  transport.snapshots.push({
    ...response,
    chat: null,
    events: [],
    ack: {action: 'steer', status: 'pending'},
  });
  store.dispatch({type: 'intent', intent: 'steer'});
  store.dispatch({type: 'draft', text: 'improve latency'});
  const steerDone = until(store, () => store.getState().submissions[1]?.status === 'done');
  store.dispatch({type: 'submit', transcriptStart: 1});
  await steerDone;
  expect(transport.requests.at(-1)).toEqual({type: 'command.steer', text: 'improve latency'});
  expect(store.getState().submissions[1]?.detail).toBe('Steer pending.');
  store.close();
  await session.close();
});

test('chat fallback and errors remain visible; stopping retires a late reply', async () => {
  const {session, transport} = sessionWith(new FakeLifecycle());
  await session.start();
  const store = createConversationStore(session, {connected: true, steerAvailable: true});
  transport.snapshots.push({
    protocol_version: 1,
    request_id: 'chat',
    timestamp: '2026-01-01T00:00:00Z',
    ok: true,
    chat: {question: 'why?', answer: 'fallback'},
  });
  store.dispatch({type: 'draft', text: 'why?'});
  const fallbackDone = until(store, () => store.getState().submissions[0]?.status === 'done');
  store.dispatch({type: 'submit', transcriptStart: 0});
  await fallbackDone;
  expect(store.getState().submissions[0]?.detail).toBe('fallback');
  transport.snapshots.push(new Error('gateway unavailable'));
  store.dispatch({type: 'draft', text: 'again'});
  const failureDone = until(store, () => store.getState().submissions[1]?.status === 'error');
  store.dispatch({type: 'submit', transcriptStart: 0});
  await failureDone;
  expect(store.getState().submissions[1]?.detail).toBe('Chat failed: gateway unavailable');
  const late = deferred<ProtocolResponse>();
  transport.snapshots.push(late.promise);
  store.dispatch({type: 'draft', text: 'old run'});
  store.dispatch({type: 'submit', transcriptStart: 0});
  store.close();
  const stopped = store.getState();
  late.resolve({
    protocol_version: 1,
    request_id: 'late',
    timestamp: '2026-01-01T00:00:00Z',
    ok: true,
    chat: {question: 'old run', answer: 'late answer'},
  });
  await late.promise;
  // The stopped actor ignores the outcome even if the transport cannot cancel it.
  await Promise.resolve();
  expect(store.getState()).toBe(stopped);
  await session.close();
});

function until(
  store: ReturnType<typeof createConversationStore>,
  predicate: () => boolean,
): Promise<void> {
  if (predicate()) return Promise.resolve();
  return new Promise(resolve => {
    const unsubscribe = store.subscribe(() => {
      if (!predicate()) return;
      unsubscribe();
      resolve();
    });
  });
}
