import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import type {GatewayBridge, GatewayCall} from '@vibesys/ipc';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  LifecycleSchema,
  PostIntentRequestSchema,
  RecordSchema,
  RelaunchRefusal,
  RunLiveness,
  TailResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {relaunchRequest} from '@vibesys/runs/intents';
import {FakeClock} from '@vibesys/transport/testing';
import {waitFor} from 'xstate';
import {intentsActor, relaunchActor} from './index.js';

/** A byte-preserving bridge: tests choose when the peer answers each owned call. */
class FakeTransport implements GatewayBridge {
  readonly calls: Array<{
    method: string;
    bytes: Uint8Array;
    resolve: (bytes: Uint8Array) => void;
    reject: (error: Error) => void;
    cancelled: boolean;
  }> = [];
  readonly streams: Array<{
    method: string;
    bytes: Uint8Array;
    receive: (bytes: Uint8Array) => void;
    ended: (error?: string) => void;
    cancelled: boolean;
  }> = [];
  unary(_host: HostKey, method: string, bytes: Uint8Array): GatewayCall<Uint8Array> {
    const {promise, resolve, reject} = Promise.withResolvers<Uint8Array>();
    const call = {method, bytes, resolve, reject, cancelled: false};
    this.calls.push(call);
    return {
      result: promise,
      cancel: () => {
        call.cancelled = true;
      },
    };
  }
  stream(
    _host: HostKey,
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string) => void,
  ) {
    const stream = {method, bytes, receive, ended, cancelled: false};
    this.streams.push(stream);
    return {
      cancel: () => {
        stream.cancelled = true;
      },
    };
  }
}

function fixture(enabled = true) {
  const bridge = new FakeTransport();
  const clock = new FakeClock();
  const actor = intentsActor({
    bridge,
    host: 'local' as HostKey,
    runId: 'run-1',
    enabled,
    schedule: clock.schedule,
  });
  actor.start();
  return {bridge, actor, clock};
}
const request = create(PostIntentRequestSchema, {
  projectRoot: '/project',
  runId: 'run-1',
  requestId: 'r-1',
  intent: {case: 'pause', value: {}},
});
function admission(requestId = 'r-1') {
  return toBinary(
    HostCallResponseSchema,
    create(HostCallResponseSchema, {
      result: {case: 'postIntent', value: {requestId, orderedReader: true}},
    }),
  );
}

test('a successful unary HostCall means pending until Tail says applied or refused', async () => {
  for (const step of [IntentStep.APPLIED, IntentStep.REFUSED]) {
    const {bridge, actor} = fixture();
    try {
      actor.send({type: 'send', request});
      const call = bridge.calls[0];
      expect(call?.method).toBe('/vibesys.api.v1.HostService/HostCall');
      const sent = fromBinary(HostCallRequestSchema, call?.bytes as Uint8Array);
      expect(sent.checkout).toBe('');
      expect(sent.method.case).toBe('postIntent');
      call?.resolve(admission());
      await waitFor(actor, snapshot => snapshot.matches({active: 'idle'}), {timeout: 1000});
      expect(actor.getSnapshot().context.entries[0]?.status).toBe('pending');
      const record = create(RecordSchema, {
        seq: 1n,
        body: {
          case: 'intent',
          value: {
            controlId: 'r-1',
            action: IntentAction.PAUSE,
            step,
            ...(step === IntentStep.REFUSED ? {reason: 'run ended'} : {}),
          },
        },
      });
      bridge.streams[0]?.receive(
        toBinary(
          TailResponseSchema,
          create(TailResponseSchema, {records: [toBinary(RecordSchema, record)]}),
        ),
      );
      expect(actor.getSnapshot().context.entries[0]?.status).toBe(
        step === IntentStep.APPLIED ? 'applied' : 'refused',
      );
    } finally {
      actor.stop();
    }
    expect(bridge.calls.every(call => call.cancelled)).toBe(true);
    expect(bridge.streams.every(stream => stream.cancelled)).toBe(true);
  }
});

test('switch off makes no transport call', () => {
  const {actor, bridge} = fixture(false);
  actor.send({type: 'send', request});
  expect(bridge.calls).toHaveLength(0);
  expect(bridge.streams).toHaveLength(0);
  actor.stop();
});

test('timeout cancels the unary and a late reply cannot settle its retry', async () => {
  const {actor, bridge, clock} = fixture();
  try {
    actor.send({type: 'send', request});
    clock.advanceBy(35_000);
    expect(bridge.calls[0]?.cancelled).toBe(true);
    expect(actor.getSnapshot().context.entries[0]?.status).toBe('uncertain');
    actor.send({type: 'retry', requestId: 'r-1'});
    expect(bridge.calls[1]?.bytes).toEqual(bridge.calls[0]?.bytes);
    bridge.calls[0]?.resolve(admission());
    bridge.calls[1]?.resolve(admission());
    await waitFor(actor, snapshot => snapshot.matches({active: 'idle'}), {timeout: 1000});
    expect(actor.getSnapshot().context.entries).toHaveLength(1);
    expect(actor.getSnapshot().context.entries[0]?.status).toBe('pending');
  } finally {
    actor.stop();
  }
});

test('typed refusals are final; malformed replies and wrong ids stay unconfirmed', async () => {
  for (const [reply, status] of [
    [new Uint8Array([255]), 'uncertain'],
    [admission('other'), 'uncertain'],
    [
      toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {
            case: 'configurationFailed',
            value: {
              code: 'invalid_input',
              stage: 'post_intent',
              message: 'unknown run',
              stderr: 'detail',
              exitCode: 2,
            },
          },
        }),
      ),
      'refused',
    ],
  ] as const) {
    const {actor, bridge} = fixture();
    try {
      actor.send({type: 'send', request});
      bridge.calls[0]?.resolve(reply);
      await waitFor(actor, snapshot => snapshot.matches({active: 'idle'}), {timeout: 1000});
      expect(actor.getSnapshot().context.entries[0]?.status).toBe(status);
      expect(actor.getSnapshot().context.error === null).toBe(status === 'refused');
    } finally {
      actor.stop();
    }
  }
});

test('a typed refusal preserves an outcome-stream failure for earlier pending requests', async () => {
  const {actor, bridge} = fixture();
  try {
    actor.send({type: 'send', request});
    bridge.streams[0]?.ended('disconnected');
    bridge.calls[0]?.resolve(
      toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {
            case: 'configurationFailed',
            value: {
              code: 'invalid_input',
              stage: 'post_intent',
              message: 'Request refused',
              exitCode: 2,
            },
          },
        }),
      ),
    );
    await waitFor(actor, snapshot => snapshot.matches({active: 'idle'}), {timeout: 1000});
    expect(actor.getSnapshot().context.entries[0]?.status).toBe('refused');
    expect(actor.getSnapshot().context.error).toBe('disconnected');
  } finally {
    actor.stop();
  }
});

test('relaunch confirms an epoch or a typed refusal and owns every transport call', async () => {
  for (const outcome of [
    {case: 'epoch', value: 2n} as const,
    {case: 'refusal', value: RelaunchRefusal.LOCKED} as const,
    {case: 'refusal', value: RelaunchRefusal.ENDED} as const,
    {case: 'refusal', value: RelaunchRefusal.CHECKOUT_MISSING} as const,
    {case: 'refusal', value: RelaunchRefusal.EPOCH_CHANGED} as const,
  ]) {
    const bridge = new FakeTransport();
    const clock = new FakeClock();
    const actor = relaunchActor({
      bridge,
      host: 'local' as HostKey,
      request: relaunchRequest('/project', 'run-1', 1n),
      observation: {
        epoch: 1n,
        lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
      },
      schedule: clock.schedule,
    });
    actor.start();
    try {
      actor.send({type: 'relaunch'});
      actor.send({type: 'relaunch'});
      expect(bridge.calls).toHaveLength(1);
      const call = bridge.calls[0];
      const sent = fromBinary(HostCallRequestSchema, call?.bytes as Uint8Array);
      expect(sent.checkout).toBe('');
      expect(sent.method).toMatchObject({
        case: 'relaunch',
        value: {runId: 'run-1', projectRoot: '/project', expectedEpoch: 1n},
      });
      call?.resolve(
        toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {case: 'relaunch', value: {outcome}},
          }),
        ),
      );
      await waitFor(actor, snapshot => snapshot.matches('settled'), {timeout: 1000});
      expect(actor.getSnapshot().context.result?.outcome).toEqual(outcome);
      actor.send({type: 'relaunch'});
      expect(bridge.calls).toHaveLength(1);
    } finally {
      actor.stop();
    }
    expect(bridge.calls.every(call => call.cancelled)).toBe(true);
    expect(bridge.streams).toHaveLength(1);
  }
});

test('relaunch deadline and owner shutdown cancel the call; late replies never settle', async () => {
  for (const mode of ['deadline', 'stop'] as const) {
    const bridge = new FakeTransport();
    const clock = new FakeClock();
    const actor = relaunchActor({
      bridge,
      host: 'local' as HostKey,
      request: relaunchRequest('/project', 'run-1', 1n),
      observation: {
        epoch: 1n,
        lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
      },
      schedule: clock.schedule,
    });
    actor.start();
    actor.send({type: 'relaunch'});
    if (mode === 'deadline') clock.advanceBy(35_000);
    else actor.stop();
    expect(bridge.calls[0]?.cancelled).toBe(true);
    bridge.calls[0]?.resolve(
      toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 2n}}},
        }),
      ),
    );
    await Promise.resolve();
    expect(actor.getSnapshot().context.result).toBeNull();
    if (mode === 'deadline') expect(actor.getSnapshot().value).toBe('uncertain');
    actor.stop();
  }
});

test('relaunch rejects malformed, missing, stale and unknown outcomes as uncertain', async () => {
  for (const bytes of [
    new Uint8Array([255]),
    admission(),
    toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {case: 'relaunch', value: {}},
      }),
    ),
    toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 1n}}},
      }),
    ),
    toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {
          case: 'relaunch',
          value: {outcome: {case: 'refusal', value: 99 as RelaunchRefusal}},
        },
      }),
    ),
  ]) {
    const bridge = new FakeTransport();
    const clock = new FakeClock();
    const actor = relaunchActor({
      bridge,
      host: 'local' as HostKey,
      request: relaunchRequest('/project', 'run-1', 1n),
      observation: {
        epoch: 1n,
        lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
      },
      schedule: clock.schedule,
    });
    actor.start();
    try {
      actor.send({type: 'relaunch'});
      bridge.calls[0]?.resolve(bytes);
      await waitFor(actor, snapshot => snapshot.matches('uncertain'), {timeout: 1000});
      expect(actor.getSnapshot().context.result).toBeNull();
    } finally {
      actor.stop();
    }
  }
});

test('Tail may observe a new epoch before HostCall acknowledges it without cancelling the launch', async () => {
  const bridge = new FakeTransport();
  const clock = new FakeClock();
  const actor = relaunchActor({
    bridge,
    host: 'local' as HostKey,
    request: relaunchRequest('/project', 'run-1', 1n),
    observation: {
      epoch: 1n,
      lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
    },
    schedule: clock.schedule,
  });
  actor.start();
  try {
    actor.send({type: 'relaunch'});
    actor.send({
      type: 'observe',
      observation: {
        epoch: 2n,
        lifecycle: create(LifecycleSchema, {state: {case: 'running', value: {}}}),
      },
    });
    expect(bridge.calls[0]?.cancelled).toBe(false);
    bridge.calls[0]?.resolve(
      toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 2n}}},
        }),
      ),
    );
    await waitFor(actor, snapshot => snapshot.matches('settled'), {timeout: 1000});
    expect(actor.getSnapshot().context.result?.outcome).toEqual({case: 'epoch', value: 2n});
    actor.send({
      type: 'observe',
      observation: {
        epoch: 2n,
        lifecycle: create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
      },
    });
    expect(actor.getSnapshot().value).toBe('ready');
    actor.send({type: 'relaunch'});
    const sent = fromBinary(HostCallRequestSchema, bridge.calls[1]?.bytes as Uint8Array);
    expect(sent.method).toMatchObject({case: 'relaunch', value: {expectedEpoch: 2n}});
  } finally {
    actor.stop();
  }
  expect(bridge.streams.every(stream => stream.cancelled)).toBe(true);
});

test('an interrupted WatchRuns hint enables relaunch only for the current run and epoch', () => {
  const bridge = new FakeTransport();
  const clock = new FakeClock();
  const actor = relaunchActor({
    bridge,
    host: 'local' as HostKey,
    request: relaunchRequest('/project', 'run-1', 2n),
    observation: {
      epoch: 2n,
      lifecycle: create(LifecycleSchema, {state: {case: 'running', value: {}}}),
    },
    schedule: clock.schedule,
  });
  actor.start();
  try {
    const watch = bridge.streams[0];
    expect(watch?.method).toBe('/vibesys.api.v1.HostService/WatchRuns');
    const hint = (runId: string, epoch: number) =>
      toBinary(
        WatchRunsResponseSchema,
        create(WatchRunsResponseSchema, {
          runId,
          liveness: RunLiveness.INTERRUPTED,
          head: {process: {epoch}},
        }),
      );
    for (const bytes of [hint('other', 2), hint('run-1', 1)]) {
      watch?.receive(bytes);
      actor.send({type: 'relaunch'});
      expect(bridge.calls).toHaveLength(0);
    }
    watch?.receive(hint('run-1', 2));
    actor.send({type: 'relaunch'});
    expect(bridge.calls).toHaveLength(1);
  } finally {
    actor.stop();
  }
  expect(bridge.streams.every(stream => stream.cancelled)).toBe(true);
});
