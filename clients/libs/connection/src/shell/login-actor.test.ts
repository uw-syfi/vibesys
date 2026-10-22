import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {LoginActor} from '@vibesys/connection';
import type {HostId} from '@vibesys/hosts';
import {LoginResponseSchema, WatchRequestSchema, WatchResponseSchema} from '@vibesys/proto';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

function startedFrame(requestId: string): Uint8Array {
  return toBinary(
    WatchResponseSchema,
    create(WatchResponseSchema, {
      result: {
        case: 'login',
        value: create(LoginResponseSchema, {
          requestId,
          sequence: 1n,
          body: {case: 'started', value: {instruction: 'Open https://a.example/x'}},
        }),
      },
    }),
  );
}

test('closing a subscription does not cancel the host login', async () => {
  let signal: AbortSignal | undefined;
  let release: () => void = () => {};
  const gate = new Promise<void>(resolve => {
    release = resolve;
  });
  const actor = new LoginActor({
    checkout: async () => '/checkout',
    stream: async (_host, bytes, owned, receive) => {
      signal = owned;
      const request = fromBinary(WatchRequestSchema, bytes);
      if (request.method.case !== 'login') throw new Error('expected login');
      receive(startedFrame(request.method.value.requestId));
      await gate;
    },
  });
  const unsubscribe = actor.subscribe(HOST, 'claude');
  const started = await actor.start(HOST, 'claude');
  unsubscribe();
  expect(actor.subscribers(HOST, 'claude')).toBe(0);
  expect(started).toEqual({outcome: 'started', instruction: 'Open https://a.example/x'});
  expect(signal?.aborted).toBe(false);
  actor.cancel(HOST, 'claude');
  expect(signal?.aborted).toBe(true);
  release();
});

test('network loss does not start the login again', async () => {
  let calls = 0;
  const actor = new LoginActor({
    checkout: async () => '/checkout',
    stream: async () => {
      calls += 1;
      throw new Error('network');
    },
  });
  await expect(actor.start(HOST, 'claude')).rejects.toThrow('network');
  await Promise.resolve();
  expect(calls).toBe(1);
});
