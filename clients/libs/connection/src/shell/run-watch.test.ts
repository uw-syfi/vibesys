import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {type RunWatchPorts, runWatch} from '@vibesys/connection';
import type {HostId} from '@vibesys/hosts';
import {RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';

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

/** A gateway stream whose frames and terminal outcome are explicit test inputs. */
class FakeRunGateway implements RunWatchPorts {
  readonly streams: {
    readonly signal: AbortSignal;
    receive(bytes: Uint8Array): void;
    end(): void;
    fail(error: Error): void;
  }[] = [];
  stream(
    _host: HostId,
    _bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void> {
    return new Promise((end, fail) => {
      this.streams.push({signal, receive, end, fail});
    });
  }
}

for (const initial of [false, true]) {
  for (const failure of [false, true]) {
    for (const restart of ['start', 'live'] as const) {
      test(`a ${failure ? 'failed' : 'ended'} stream reconnects through ${restart}, initial=${initial}`, async () => {
        const gateway = new FakeRunGateway();
        const watch = runWatch(gateway);
        const first = watch.live(HOST);
        const old = gateway.streams[0];
        if (old === undefined) throw new Error('missing first stream');
        old.receive(frame({runId: 'stale', liveness: RunLiveness.ALIVE}));
        if (initial) old.receive(frame({initialComplete: true}));
        if (failure) old.fail(new Error('network loss'));
        else old.end();
        await first;
        await Promise.resolve();
        if (restart === 'start') watch.start(HOST);
        const live = watch.live(HOST);
        expect(gateway.streams.length).toBe(2);
        const fresh = gateway.streams[1];
        if (fresh === undefined) throw new Error('missing replacement stream');
        old.receive(frame({runId: 'late', liveness: RunLiveness.ALIVE}));
        fresh.receive(frame({initialComplete: true}));
        expect(await live).toBe(0);
        fresh.receive(frame({runId: 'fresh', liveness: RunLiveness.ALIVE}));
        expect(await watch.live(HOST)).toBe(1);
        watch.close();
        expect(fresh.signal.aborted).toBe(true);
        fresh.end();
      });
    }
  }
}

test('an old cancelled stream settling cannot remove its replacement', async () => {
  const gateway = new FakeRunGateway();
  const watch = runWatch(gateway);
  watch.start(HOST);
  watch.cancel(HOST);
  const live = watch.live(HOST);
  gateway.streams[0]?.end();
  await Promise.resolve();
  gateway.streams[1]?.receive(frame({runId: 'fresh', liveness: RunLiveness.ALIVE}));
  gateway.streams[1]?.receive(frame({initialComplete: true}));
  expect(await live).toBe(1);
  expect(await watch.live(HOST)).toBe(1);
  expect(gateway.streams.length).toBe(2);
  watch.close();
  gateway.streams[1]?.end();
});
