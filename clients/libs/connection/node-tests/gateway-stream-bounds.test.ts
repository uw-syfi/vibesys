/**
 * What bounds a watch stream on the client: the registry's budgets, enforced on the connection's
 * injected clock. A gateway that stops serving while the link stays up must not leave a call pending.
 */
import {test} from 'node:test';
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  encodeHostWatch,
  LoginRequestSchema,
  StreamSavedRunsRequestSchema,
  WatchResponseSchema,
  watchBudgets,
} from '@vibesys/proto';
import {GrpcCode, grpcCode} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {CallTimedOut, GatewayCalls, GatewayConnection, WATCH_METHOD} from '../src/index.js';
import {FakeGateway, type FakeWatch} from '../src/testing/fake-gateway.js';

interface Slot {
  readonly name: string;
  readonly request: Uint8Array;
  readonly frame: Uint8Array;
}

function watchFrame(result: NonNullable<MessageInitShape<typeof WatchResponseSchema>['result']>) {
  return toBinary(WatchResponseSchema, create(WatchResponseSchema, {result}));
}

/** One request and one valid reply per watch slot; a new slot in the registry needs a row here. */
const SLOTS = {
  login: {
    name: 'Login',
    request: encodeHostWatch(
      '/checkout',
      'Login',
      toBinary(
        LoginRequestSchema,
        create(LoginRequestSchema, {requestId: 'r', provider: 'claude'}),
      ),
    ),
    frame: watchFrame({
      case: 'login',
      value: {requestId: 'r', sequence: 1n, body: {case: 'started', value: {}}},
    }),
  },
  streamSavedRuns: {
    name: 'StreamSavedRuns',
    request: encodeHostWatch(
      '/checkout',
      'StreamSavedRuns',
      toBinary(
        StreamSavedRunsRequestSchema,
        create(StreamSavedRunsRequestSchema, {requestId: 'r', project: '/project'}),
      ),
    ),
    frame: watchFrame({
      case: 'streamSavedRuns',
      value: {requestId: 'r', projectRoot: '/project', runs: [], complete: false},
    }),
  },
} as const satisfies Record<keyof typeof watchBudgets, Slot>;

/** A stream open to a fake gateway, on a clock the test advances. */
async function open(slot: keyof typeof watchBudgets) {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const connection = new GatewayConnection(fake, clock.schedule);
  const abort = new AbortController();
  let heard = 0;
  let notify = (): void => {};
  const outcome = connection
    .stream(WATCH_METHOD, SLOTS[slot].request, abort.signal, () => {
      heard += 1;
      notify();
    })
    .then(
      () => 'ended' as const,
      (error: unknown) => error,
    );
  const watch = await fake.nextWatch();
  return {
    clock,
    abort,
    watch,
    outcome,
    /** Send the slot's frame and wait for the connection to accept it. */
    async deliver(peer: FakeWatch = watch): Promise<void> {
      const target = heard + 1;
      const arrived = new Promise<void>(resolve => {
        notify = () => heard >= target && resolve();
      });
      peer.send(SLOTS[slot].frame);
      await arrived;
    },
    /** Whether the call has settled once everything already in flight has run; the clock is fake, so nothing else can settle it. */
    settled: async (): Promise<boolean> => {
      let done = false;
      void outcome.then(() => {
        done = true;
      });
      await new Promise(resolve => setImmediate(resolve));
      return done;
    },
    close(): void {
      abort.abort();
      connection.close();
      fake.close();
    },
  };
}

for (const [slot, budget] of Object.entries(watchBudgets) as [
  keyof typeof watchBudgets,
  (typeof watchBudgets)[keyof typeof watchBudgets],
][]) {
  const silenceMs = budget.progressMs > 0 ? budget.progressMs + budget.graceMs : undefined;
  const firstMs = budget.firstEventMs > 0 ? budget.firstEventMs + budget.graceMs : silenceMs;

  test(`${slot} gives its first frame the registry window, then ends with a typed deadline`, {
    timeout: 15000,
  }, async () => {
    expect(firstMs).toBeDefined();
    const call = await open(slot);
    try {
      await call.clock.waitUntilScheduled(firstMs as number);
      call.clock.advanceBy((firstMs as number) - 1);
      expect(await call.settled()).toBe(false);
      call.clock.advanceBy(1);
      const error = await call.outcome;
      expect(error).toBeInstanceOf(CallTimedOut);
      expect((error as CallTimedOut).bound).toBe('first-frame');
      expect(grpcCode(error)).toBe(GrpcCode.DEADLINE_EXCEEDED);
      await call.watch.closed;
      expect(call.clock.pendingDelays()).toEqual([]);
    } finally {
      call.close();
    }
  });

  if (silenceMs !== undefined) {
    test(`${slot} ends with a typed deadline when frames stop for the registry's silence`, {
      timeout: 15000,
    }, async () => {
      const call = await open(slot);
      try {
        await call.deliver();
        await call.clock.waitUntilScheduled(silenceMs);
        call.clock.advanceBy(silenceMs - 1);
        expect(await call.settled()).toBe(false);
        call.clock.advanceBy(1);
        const error = await call.outcome;
        expect(error).toBeInstanceOf(CallTimedOut);
        expect((error as CallTimedOut).bound).toBe('silence');
        expect(grpcCode(error)).toBe(GrpcCode.DEADLINE_EXCEEDED);
        await call.watch.closed;
        expect(call.clock.pendingDelays()).toEqual([]);
      } finally {
        call.close();
      }
    });

    test(`${slot} lives past the silence window while each gap stays inside it, and ends at its cap`, {
      timeout: 15000,
    }, async () => {
      const call = await open(slot);
      try {
        const gap = silenceMs - 1;
        let elapsed = 0;
        await call.deliver();
        while (elapsed + gap < budget.totalMs) {
          call.clock.advanceBy(gap);
          elapsed += gap;
          expect(await call.settled()).toBe(false);
          await call.deliver();
        }
        expect(elapsed).toBeGreaterThan(silenceMs);
        call.clock.advanceBy(budget.totalMs - elapsed);
        const error = await call.outcome;
        expect(error).toBeInstanceOf(CallTimedOut);
        expect((error as CallTimedOut).bound).toBe('total');
      } finally {
        call.close();
      }
    });
  } else {
    test(`${slot} guards only its first frame and then runs to its cap`, {
      timeout: 15000,
    }, async () => {
      const call = await open(slot);
      try {
        await call.deliver();
        expect(call.clock.pendingDelays()).toEqual([budget.totalMs]);
        call.clock.advanceBy(budget.totalMs - 1);
        expect(await call.settled()).toBe(false);
        call.clock.advanceBy(1);
        expect(((await call.outcome) as CallTimedOut).bound).toBe('total');
      } finally {
        call.close();
      }
    });
  }

  test(`${slot} leaves no timer behind when it is cancelled or when the gateway ends it`, {
    timeout: 15000,
  }, async () => {
    const cancelled = await open(slot);
    try {
      await cancelled.deliver();
      cancelled.abort.abort();
      expect(await cancelled.outcome).toBeInstanceOf(Error);
      await cancelled.watch.closed;
      expect(cancelled.clock.pendingDelays()).toEqual([]);
    } finally {
      cancelled.close();
    }
    const ended = await open(slot);
    try {
      await ended.deliver();
      ended.watch.end();
      expect(await ended.outcome).toBe('ended');
      expect(ended.clock.pendingDelays()).toEqual([]);
    } finally {
      ended.close();
    }
  });
}

test('a listing over a gateway that stopped serving fails in the registry bound, not never', {
  timeout: 15000,
}, async () => {
  const {firstEventMs, progressMs, graceMs} = watchBudgets.streamSavedRuns;
  const firstFrameMs = (firstEventMs > 0 ? firstEventMs : progressMs) + graceMs;
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.schedule);
  const outcome = calls
    .stream(
      'local',
      WATCH_METHOD,
      SLOTS.streamSavedRuns.request,
      new AbortController().signal,
      () => {},
    )
    .then(
      () => 'ended',
      (error: unknown) => error,
    );
  try {
    const watch = await fake.nextWatch();
    await clock.waitUntilScheduled(firstFrameMs);
    clock.advanceBy(firstFrameMs);
    const error = await outcome;
    expect(error).toBeInstanceOf(CallTimedOut);
    expect((error as CallTimedOut).bound).toBe('first-frame');
    await watch.closed;
    expect(clock.pendingDelays()).toEqual([]);
  } finally {
    calls.close();
    fake.close();
  }
});
