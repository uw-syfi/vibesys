import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {
  ChatMessageRequestSchema,
  ChatRole,
  HostCallRequestSchema,
  HostCallResponseSchema,
  RecordSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {waitFor} from 'xstate';
import {sessionsActor} from './index.js';

const request = create(ChatMessageRequestSchema, {
  projectRoot: '/project',
  runId: 'run-a',
  sessionId: 'default',
  requestId: 'q-1',
  text: 'Why?',
});
function fixture(enabled = true) {
  const bridge = new FakeGatewayBridge();
  const clock = new FakeClock();
  const actor = sessionsActor({
    bridge,
    host: 'local' as HostKey,
    runId: 'run-a',
    sessionId: 'default',
    enabled,
    schedule: clock.schedule,
  });
  actor.start();
  if (enabled) actor.send({type: 'toggle'});
  return {actor, bridge, clock};
}
test('per-host switch prevents every call; leaving cancels a Tail and a pending post', () => {
  const off = fixture(false);
  expect(off.bridge.streams).toHaveLength(0);
  off.actor.stop();
  const {actor, bridge} = fixture();
  actor.send({type: 'send', request});
  const tail = fromBinary(TailRequestSchema, bridge.streams[0]?.bytes as Uint8Array);
  expect(tail.sessionId).toBe('default');
  const sent = fromBinary(HostCallRequestSchema, bridge.calls[0]?.bytes as Uint8Array);
  expect(sent.method.case).toBe('chatMessage');
  actor.stop();
  expect(bridge.calls[0]?.cancelled).toBe(true);
  expect(bridge.streams[0]?.cancelled).toBe(true);
});
test('late admission cannot overwrite a timed-out retry, and retry keeps its id', async () => {
  const {actor, bridge, clock} = fixture();
  try {
    actor.send({type: 'send', request});
    clock.advanceBy(35000);
    actor.send({type: 'retry'});
    expect(bridge.calls[0]?.bytes).toEqual(bridge.calls[1]?.bytes);
    const response = toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {case: 'chatMessage', value: {requestId: 'q-1', sessionId: 'default'}},
      }),
    );
    bridge.calls[0]?.resolve(response);
    expect(actor.getSnapshot().matches({active: {sending: 'posting'}})).toBe(true);
    bridge.calls[1]?.resolve(response);
    await waitFor(actor, state => state.matches({active: {sending: 'idle'}}), {timeout: 1000});
    expect(actor.getSnapshot().context.uncertain).toBe(false);
  } finally {
    actor.stop();
  }
});
test('reconnect resumes the session sequence and preserves messages', async () => {
  const {actor, bridge, clock} = fixture();
  try {
    const record = create(RecordSchema, {
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
    });
    bridge.streams[0]?.receive(
      toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {records: [toBinary(RecordSchema, record)]}),
      ),
    );
    await waitFor(actor, state => state.context.history.throughSeq === 1n, {timeout: 1000});
    bridge.streams[0]?.ended('lost');
    clock.advanceBy(1000);
    const tail = fromBinary(TailRequestSchema, bridge.streams[1]?.bytes as Uint8Array);
    expect(tail.fromSeq).toBe(2n);
    expect(tail.sessionId).toBe('default');
    expect(actor.getSnapshot().context.history.turns[0]?.question).toBe('Why?');
  } finally {
    actor.stop();
  }
});
