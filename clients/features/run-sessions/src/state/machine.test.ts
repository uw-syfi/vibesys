import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {
  ChatMessageRequestSchema,
  ChatMessageResponseSchema,
  ChatRole,
  RecordSchema,
} from '@vibesys/proto';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {emptyHistory, foldChat, runSessionsMachine} from './index.js';

const request = create(ChatMessageRequestSchema, {
  projectRoot: '/project',
  runId: 'run-a',
  sessionId: 'default',
  requestId: 'q-1',
  text: 'Why?',
});

test('disabled chat starts no work and cannot post', () => {
  const off = getNextSnapshot(
    runSessionsMachine,
    getInitialSnapshot(runSessionsMachine, {enabled: false}),
    {type: 'send', request},
  );
  expect(off.value).toBe('disabled');
  expect(off.children).toEqual({});
});
test('deadline preserves the exact question identity for an explicit retry', () => {
  const sending = getNextSnapshot(
    runSessionsMachine,
    getNextSnapshot(runSessionsMachine, getInitialSnapshot(runSessionsMachine, {enabled: true}), {
      type: 'toggle',
    }),
    {type: 'send', request},
  );
  const timedOut = getNextSnapshot(runSessionsMachine, sending, {
    type: 'xstate.after.deadline.runSessions.active.sending.posting',
  } as never);
  expect(timedOut.context.uncertain).toBe(true);
  const retry = getNextSnapshot(runSessionsMachine, timedOut, {type: 'retry'});
  expect(retry.context.current).toEqual(request);
  expect(retry.matches({active: {sending: 'posting'}})).toBe(true);
});
test('model: every live operation and delay has an active owner', () => {
  checkMachine({
    machine: runSessionsMachine,
    input: {enabled: true},
    event: (random, live) =>
      random.pick([
        {type: 'send', request},
        {type: 'toggle'},
        {type: 'retry'},
        {type: 'disconnected'},
        {type: 'records', records: []},
        ...live.operations
          .filter(operation => operation === 'post')
          .map(operation =>
            live.done(
              operation,
              create(ChatMessageResponseSchema, {requestId: 'q-1', sessionId: 'default'}),
            ),
          ),
        ...live.operations
          .filter(operation => operation === 'resolve')
          .map(operation => live.done(operation, {records: [], blobs: new Map<string, string>()})),
        ...live.operations.map(operation => live.error(operation, new Error('failed'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot =>
      snapshot.context.history.throughSeq >= 0n ? [] : ['negative sequence'],
  });
});
test('fold replaces streamed tokens with the terminal answer and ignores replay', () => {
  const records = [
    create(RecordSchema, {
      seq: 1n,
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {requestId: 'q-1', role: ChatRole.USER, content: {case: 'text', value: 'Why?'}},
          },
        },
      },
    }),
    create(RecordSchema, {
      seq: 2n,
      body: {
        case: 'chat',
        value: {event: {case: 'token', value: {requestId: 'q-1', text: 'Because'}}},
      },
    }),
    create(RecordSchema, {
      seq: 3n,
      body: {
        case: 'chat',
        value: {
          event: {
            case: 'message',
            value: {
              requestId: 'q-1',
              role: ChatRole.ASSISTANT,
              content: {case: 'text', value: 'Because it won.'},
            },
          },
        },
      },
    }),
  ];
  const batch = {records, blobs: new Map<string, string>()};
  const streamed = foldChat(emptyHistory, {...batch, records: records.slice(0, 2)});
  expect(streamed.turns[0]?.answer).toBe('Because');
  expect(streamed.turns[0]?.streaming).toBe(true);
  const final = foldChat(streamed, batch);
  expect(final.turns[0]?.answer).toBe('Because it won.');
  expect(final.turns[0]?.streaming).toBe(false);
  expect(foldChat(final, batch)).toEqual(final);
  expect(() => foldChat(emptyHistory, {...batch, records: records.slice(1)})).toThrow('gap');
});

test('a stalled content read has a bounded queue and can reopen from its last fold', () => {
  let snapshot = getNextSnapshot(
    runSessionsMachine,
    getInitialSnapshot(runSessionsMachine, {enabled: true}),
    {type: 'toggle'},
  );
  for (let index = 0; index < 200; index++) {
    snapshot = getNextSnapshot(runSessionsMachine, snapshot, {type: 'records', records: []});
    expect(snapshot.context.queue.length).toBeLessThanOrEqual(64);
  }
  expect(snapshot.context.contentFailed).toBe(true);
  expect(snapshot.context.queue).toEqual([]);
  snapshot = getNextSnapshot(runSessionsMachine, snapshot, {type: 'toggle'});
  snapshot = getNextSnapshot(runSessionsMachine, snapshot, {type: 'toggle'});
  expect(snapshot.context.contentFailed).toBe(false);
});
