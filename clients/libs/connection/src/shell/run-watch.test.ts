import {expect, test} from 'bun:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  RUN_WATCH_LEASE_MS,
  type RunsState,
  type RunWatchPorts,
  runWatch,
} from '@vibesys/connection';

const watching = (ports: RunWatchPorts) => runWatch(ports, new FakeClock().schedule);

import type {HostId} from '@vibesys/hosts';
import {RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {FakeRunGateway} from '../testing/index.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

function frame(value: {
  heartbeat?: boolean;
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
  const watch = watching({
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
  const again = watching({
    stream: (_host, _bytes, _owned, accept) => {
      accept(frame({initialComplete: true}));
      return Promise.resolve();
    },
  });
  expect(await again.live(HOST)).toBe(0);
});

for (const initial of [false, true]) {
  for (const failure of [false, true]) {
    for (const restart of ['start', 'live'] as const) {
      test(`a ${failure ? 'failed' : 'ended'} stream reconnects through ${restart}, initial=${initial}`, async () => {
        const gateway = new FakeRunGateway();
        const watch = watching(gateway);
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
  const watch = watching(gateway);
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

function observed(watch: ReturnType<typeof runWatch>, host: HostId): RunsState[] {
  const states: RunsState[] = [];
  watch.observe(host, state => states.push(state));
  return states;
}

test('an observer hears the stream connect, deliver every run, then each change', () => {
  const gateway = new FakeRunGateway();
  const watch = runWatch(gateway, new FakeClock().schedule);
  const states = observed(watch, HOST);
  const stream = gateway.streams[0];
  stream?.receive(frame({runId: 'run-1', liveness: RunLiveness.ALIVE}));
  expect(states.map(state => state.phase.kind)).toEqual(['connecting']);
  stream?.receive(frame({initialComplete: true}));
  stream?.receive(frame({runId: 'run-2', liveness: RunLiveness.ENDED}));
  stream?.receive(
    toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {heartbeat: true})),
  );
  stream?.receive(frame({runId: 'run-1', removed: true}));
  expect(states.map(state => [state.phase.kind, [...state.runs.keys()]])).toEqual([
    ['connecting', []],
    ['live', ['run-1']],
    ['live', ['run-1', 'run-2']],
    ['live', ['run-2']],
  ]);
  watch.close();
});

/** Lets a stream's settled promise reach the watch. */
const settle = async () => {
  for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
};

test('an observed host reconnects with a growing delay, and a stream that speaks resets it', async () => {
  const clock = new FakeClock();
  const gateway = new FakeRunGateway();
  const watch = runWatch(gateway, clock.schedule);
  const states = observed(watch, HOST);
  const delays = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000];
  for (const [index, delay] of delays.entries()) {
    gateway.streams[index]?.fail(new Error('network loss'));
    await settle();
    expect(states.at(-1)?.phase).toMatchObject({kind: 'reconnecting', attempt: index + 1});
    expect(gateway.streams).toHaveLength(index + 1);
    clock.advanceBy(delay - 1);
    expect(gateway.streams).toHaveLength(index + 1);
    clock.advanceBy(1);
    expect(gateway.streams).toHaveLength(index + 2);
  }
  gateway.streams.at(-1)?.receive(frame({runId: 'run-1', liveness: RunLiveness.ALIVE}));
  gateway.streams.at(-1)?.receive(frame({initialComplete: true}));
  expect(states.at(-1)?.phase).toEqual({kind: 'live'});
  gateway.streams.at(-1)?.end();
  await settle();
  expect(states.at(-1)?.phase).toMatchObject({kind: 'reconnecting', attempt: 1});
  expect(states.at(-1)?.runs.size).toBe(0);
  watch.close();
});

test('a stream silent for the protocol lease is dropped and opened again; frames renew it', async () => {
  const clock = new FakeClock();
  const gateway = new FakeRunGateway();
  const watch = runWatch(gateway, clock.schedule);
  const states = observed(watch, HOST);
  gateway.streams[0]?.receive(frame({initialComplete: true}));
  clock.advanceBy(RUN_WATCH_LEASE_MS - 1);
  gateway.streams[0]?.receive(frame({heartbeat: true}));
  clock.advanceBy(RUN_WATCH_LEASE_MS - 1);
  expect(gateway.streams[0]?.signal.aborted).toBe(false);
  clock.advanceBy(1);
  expect(gateway.streams[0]?.signal.aborted).toBe(true);
  expect(states.at(-1)?.phase).toMatchObject({kind: 'reconnecting', attempt: 1});
  clock.advanceBy(1_000);
  expect(gateway.streams).toHaveLength(2);
  watch.close();
  expect(clock.pending).toBe(0);
});

test('cancel stops an observed host for good: no reconnect, no listener call', async () => {
  const clock = new FakeClock();
  const gateway = new FakeRunGateway();
  const watch = runWatch(gateway, clock.schedule);
  const states = observed(watch, HOST);
  watch.cancel(HOST);
  gateway.streams[0]?.fail(new Error('late'));
  await settle();
  clock.advanceBy(120_000);
  expect(gateway.streams).toHaveLength(1);
  expect(states).toHaveLength(1);
  watch.close();
  expect(clock.pending).toBe(0);
});
