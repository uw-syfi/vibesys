import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';
import {runWatch} from './run-watch.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

function frame(value: {
  runId?: string;
  liveness?: RunLiveness;
  removed?: boolean;
  initialComplete?: boolean;
}): Uint8Array {
  return toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, value));
}

test('a live run counts and cancel aborts the stream before a later frame', async () => {
  let signal: AbortSignal | undefined;
  let receive: ((bytes: Uint8Array) => void) | undefined;
  const watch = runWatch({
    stream: (_host, _bytes, owned, accept) => {
      signal = owned;
      receive = accept;
      accept(frame({runId: 'run-1', liveness: RunLiveness.ALIVE}));
      accept(frame({runId: 'run-2', liveness: RunLiveness.ENDED}));
      accept(frame({initialComplete: true}));
      return new Promise((_resolve, reject) => {
        owned.addEventListener('abort', () => reject(new Error('cancelled')));
      });
    },
  });

  expect(await watch.live(HOST)).toBe(1);
  watch.cancel(HOST);
  expect(signal?.aborted).toBe(true);
  receive?.(frame({runId: 'run-3', liveness: RunLiveness.ALIVE}));
  const again = runWatch({
    stream: (_host, _bytes, _owned, accept) => {
      accept(frame({initialComplete: true}));
      return Promise.resolve();
    },
  });
  expect(await again.live(HOST)).toBe(0);
});
