/**
 * A host that goes silent while its SSH link stays up, through the listing's production wiring:
 * `registrySavedRuns` over `GatewayCalls`, a real in-process HTTP/2 peer, and a clock the test
 * advances. The windows are the registry's (`watchBudgets.streamSavedRuns`: the gateway's 60 s to
 * the first frame and 25 s between frames, each plus `grace_ms` for the gateway's own typed answer),
 * so no test measures time and none can flake.
 */
import {test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {SavedRunImport, WatchResponseSchema, watchBudgets} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {
  CallTimedOut,
  GatewayCalls,
  registrySavedRuns,
  SavedRunsInterrupted,
  WATCH_METHOD,
} from '../src/index.js';
import {FakeGateway, type FakeWatch} from '../src/testing/fake-gateway.js';

const HOST: HostId = {kind: 'local'};
const {firstEventMs, progressMs, graceMs} = watchBudgets.streamSavedRuns;
const FIRST_FRAME_MS = firstEventMs + graceMs;
const SILENCE_MS = progressMs + graceMs;

/** The listing, as the app wires it, against a gateway whose host the test controls. */
async function listing() {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.schedule);
  let heard = 0;
  let notify = (): void => {};
  const reads = registrySavedRuns({
    async checkout() {
      return '/checkout';
    },
    stream: (_host, bytes, signal, receive) =>
      calls.stream('local', WATCH_METHOD, bytes, signal, payload => {
        receive(payload);
        heard += 1;
        notify();
      }),
  });
  const outcome = reads.list(HOST, '/project', new AbortController().signal).then(
    runs => ({runs}),
    (error: unknown) => ({error}),
  );
  const watch = await fake.nextWatch();
  if (watch.request.method.case !== 'streamSavedRuns') throw new Error('unexpected watch');
  const {requestId} = watch.request.method.value;
  return {
    clock,
    watch,
    outcome,
    /** Send a frame of runs `ids` and wait until the listing has taken it. */
    async frame(ids: readonly string[], complete = false, peer: FakeWatch = watch): Promise<void> {
      const target = heard + 1;
      const taken = new Promise<void>(resolve => {
        notify = () => heard >= target && resolve();
      });
      peer.send(
        toBinary(
          WatchResponseSchema,
          create(WatchResponseSchema, {
            result: {
              case: 'streamSavedRuns',
              value: {
                requestId,
                projectRoot: '/project',
                complete,
                runs: ids.map((runId, at) => ({
                  runId,
                  displayName: runId,
                  createdAt: {seconds: BigInt(1_000 + at)},
                  imported: SavedRunImport.AVAILABLE,
                })),
              },
            },
          }),
        ),
      );
      await taken;
    },
    /** Whether the listing has settled once everything already in flight has run. */
    async settled(): Promise<boolean> {
      let done = false;
      void outcome.then(() => {
        done = true;
      });
      await new Promise(resolve => setImmediate(resolve));
      return done;
    },
    close(): void {
      calls.close();
      fake.close();
    },
  };
}

test('a host silent before its first frame fails the listing at the first-frame window, naming it', {
  timeout: 15000,
}, async () => {
  const run = await listing();
  try {
    await run.clock.waitUntilScheduled(FIRST_FRAME_MS);
    run.clock.advanceBy(FIRST_FRAME_MS - 1);
    expect(await run.settled()).toBe(false);
    run.clock.advanceBy(1);
    const result = await run.outcome;
    expect('error' in result && result.error).toBeInstanceOf(CallTimedOut);
    expect('error' in result && (result.error as CallTimedOut).bound).toBe('first-frame');
    // No frame arrived, so this is the call's own failure, not a partial listing.
    expect('error' in result && result.error).not.toBeInstanceOf(SavedRunsInterrupted);
    await run.watch.closed;
    expect(run.clock.pendingDelays()).toEqual([]);
  } finally {
    run.close();
  }
});

test('a host that falls silent after some frames fails the listing and keeps the rows it sent', {
  timeout: 15000,
}, async () => {
  const run = await listing();
  try {
    await run.frame(['run-a', 'run-b']);
    await run.clock.waitUntilScheduled(SILENCE_MS);
    run.clock.advanceBy(SILENCE_MS - 1);
    expect(await run.settled()).toBe(false);
    run.clock.advanceBy(1);
    const result = await run.outcome;
    const error = 'error' in result ? result.error : undefined;
    expect(error).toBeInstanceOf(SavedRunsInterrupted);
    expect((error as SavedRunsInterrupted).runs.map(each => each.runId).sort()).toEqual([
      'run-a',
      'run-b',
    ]);
    expect((error as SavedRunsInterrupted).cause).toBeInstanceOf(CallTimedOut);
    expect(((error as SavedRunsInterrupted).cause as CallTimedOut).bound).toBe('silence');
    await run.watch.closed;
    expect(run.clock.pendingDelays()).toEqual([]);
  } finally {
    run.close();
  }
});

test('a slow host whose every gap is inside the window lists everything, however long it takes', {
  timeout: 15000,
}, async () => {
  const run = await listing();
  try {
    // The first frame takes almost the whole first-frame window, which is longer than the silence
    // window, and every later gap is one tick short of the silence window.
    run.clock.advanceBy(FIRST_FRAME_MS - 1);
    await run.frame(['run-0']);
    let elapsed = FIRST_FRAME_MS - 1;
    for (let at = 1; at <= 6; at += 1) {
      run.clock.advanceBy(SILENCE_MS - 1);
      elapsed += SILENCE_MS - 1;
      expect(await run.settled()).toBe(false);
      await run.frame([`run-${at}`], at === 6);
    }
    expect(elapsed).toBeGreaterThan(FIRST_FRAME_MS + 5 * (SILENCE_MS - 1));
    const result = await run.outcome;
    expect('runs' in result && result.runs.map(each => each.runId)).toEqual([
      'run-6',
      'run-5',
      'run-4',
      'run-3',
      'run-2',
      'run-1',
      'run-0',
    ]);
    // The completing frame ends the listing, and the stream with it.
    await run.watch.closed;
    expect(run.clock.pendingDelays()).toEqual([]);
  } finally {
    run.close();
  }
});
