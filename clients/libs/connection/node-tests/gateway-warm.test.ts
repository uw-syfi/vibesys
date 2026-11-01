/**
 * A session to a host can be started ahead of its first call. The desktop app starts This Mac's
 * gateway (0.3 s on its first run) while its window loads, so the two wait side by side and the
 * first read waits for the longer, not for both.
 */
import {test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {encodeHostRead, ListTasksRequestSchema, ReadResponseSchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {GatewayCalls, READ_METHOD} from '../src/index.js';
// test-isolation: Node HTTP/2 needs the gateway Fake alone; legacy contract exports load bun:test.
import {FakeGateway} from '../src/testing/fake-gateway.js';

const HOLD_MS = 5_000;

const asking = (): Uint8Array =>
  encodeHostRead(
    '/checkout',
    'ListTasks',
    toBinary(
      ListTasksRequestSchema,
      create(ListTasksRequestSchema, {requestId: 'read', project: 'project-a'}),
    ),
  );

const answer = (): Uint8Array =>
  toBinary(
    ReadResponseSchema,
    create(ReadResponseSchema, {
      result: {
        case: 'listTasks',
        value: {
          requestId: 'read',
          tasks: {version: 1n, projectRoot: 'project-a', tasks: [{name: 'task'}]},
        },
      },
    }),
  );

function peer() {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.schedule);
  return {
    fake,
    clock,
    calls,
    ask: () => calls.unary('local', READ_METHOD, asking(), new AbortController().signal),
    end: () => {
      calls.close();
      fake.close();
    },
  };
}

test('a warmed host is dialed before any call, and the first call finds that session', {
  timeout: 15000,
}, async () => {
  const {fake, calls, ask, end} = peer();
  try {
    calls.warm('local', HOLD_MS);
    await fake.nextDial();
    expect(fake.readsReceived).toHaveLength(0);
    const reply = ask();
    (await fake.nextRead()).respond(answer());
    await reply;
    expect(fake.forces).toEqual([false]);
  } finally {
    end();
  }
});

test('a warmed session nobody uses is closed when the hold ends, and a later call dials again', {
  timeout: 15000,
}, async () => {
  const {fake, clock, calls, ask, end} = peer();
  try {
    calls.warm('local', HOLD_MS);
    await fake.nextDial();
    await clock.waitUntilScheduled(HOLD_MS);
    clock.advanceBy(HOLD_MS);
    expect(clock.pendingDelays()).not.toContain(HOLD_MS);
    const reply = ask();
    await fake.nextDial();
    (await fake.nextRead()).respond(answer());
    await reply;
    expect(fake.forces).toEqual([false, false]);
  } finally {
    end();
  }
});

test('a call in flight keeps the warmed session past the hold', {timeout: 15000}, async () => {
  const {fake, clock, calls, ask, end} = peer();
  try {
    calls.warm('local', HOLD_MS);
    const reply = ask();
    const read = await fake.nextRead();
    await clock.waitUntilScheduled(HOLD_MS);
    clock.advanceBy(HOLD_MS);
    read.respond(answer());
    await reply;
    expect(fake.forces).toEqual([false]);
  } finally {
    end();
  }
});

test('closing the calls ends the hold without waiting for it', {timeout: 15000}, async () => {
  const {fake, clock, calls, end} = peer();
  try {
    calls.warm('local', HOLD_MS);
    await fake.nextDial();
    calls.close();
    expect(clock.pendingDelays()).not.toContain(HOLD_MS);
    calls.warm('local', HOLD_MS);
    expect(clock.pendingDelays()).not.toContain(HOLD_MS);
  } finally {
    end();
  }
});
