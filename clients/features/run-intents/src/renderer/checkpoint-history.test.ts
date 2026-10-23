import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {
  IntentAction,
  IntentStep,
  KeyframeSchema,
  RecordSchema,
  SkeletonResponseSchema,
  type Record as StoreRecord,
  TailRequestSchema,
} from '@vibesys/proto';
import {foldSkeleton, recordsFanout, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {intentOutcomes} from '@vibesys/runs/intents';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {waitFor} from 'xstate';
import {foldOutcomes} from '../state/index.js';
import {intentsActor} from './index.js';

const TAIL = '/vibesys.api.v1.HostService/Tail';

/** A saved checkpoint is a byte response, while the inherited Fake owns real Tail semantics. */
class CheckpointPeer extends FakeRunTransport {
  constructor(
    records: readonly StoreRecord[],
    private readonly keyframe: Uint8Array | undefined,
    private readonly cut: number,
  ) {
    super({records});
  }

  override async unary(method: string, request: Uint8Array, signal: AbortSignal) {
    if (!method.endsWith('/Skeleton') || this.keyframe === undefined)
      return super.unary(method, request, signal);
    this.calls.push({method, request, signal});
    return toBinary(
      SkeletonResponseSchema,
      create(SkeletonResponseSchema, {
        keyframe: this.keyframe,
        committedSeq: BigInt(this.cut),
        nextSeq: BigInt(this.cut + 1),
        complete: true,
      }),
    );
  }
}

function log(length: number): StoreRecord[] {
  return Array.from({length}, (_, i) => {
    const seq = BigInt(i + 1);
    const epoch = i < 4 ? 1 : 2;
    const record = create(RecordSchema, {seq, epoch, at: {seconds: seq}});
    if (i === 0 || i === 4)
      record.body = create(RecordSchema, {
        body: {case: 'epochStarted', value: {epoch, runId: 'run-a'}},
      }).body;
    else if (i === 1 || i === 2)
      record.body = create(RecordSchema, {
        body: {
          case: 'intent',
          value: {
            controlId: `intent-${i}`,
            action: IntentAction.STEER,
            step: i === 1 ? IntentStep.APPLIED : IntentStep.REFUSED,
            ...(i === 2 ? {reason: 'epoch closed'} : {}),
          },
        },
      }).body;
    else
      record.body = create(RecordSchema, {
        body: {case: 'notice', value: {severity: 1, code: 1, message: 'later work'}},
      }).body;
    return record;
  });
}

function checkpoint(records: readonly StoreRecord[], cut: number, previous: boolean) {
  if (cut === 0) return undefined;
  const state = foldSkeleton(seedSkeleton(), records.slice(0, cut));
  const frame = create(KeyframeSchema, {...state, throughSeq: BigInt(cut), logOffset: 8n});
  if (previous) {
    frame.intents = [];
    frame.recordFacts = [];
    frame.recordsTracked = false;
    for (const version of frame.epochs) {
      version.startsEpoch = false;
      version.startedAt = undefined;
    }
  }
  return toBinary(KeyframeSchema, frame);
}

async function exercise(records: StoreRecord[], keyframe: Uint8Array | undefined, cut: number) {
  const peer = new CheckpointPeer(records, keyframe, cut);
  const clock = new FakeClock();
  const fanout = recordsFanout();
  const makeActor = () =>
    intentsActor({
      bridge: new FakeGatewayBridge(),
      host: 'local',
      runId: 'run-a',
      enabled: true,
      schedule: clock.schedule,
      records: fanout.subscribe,
    }).start();
  let actor = makeActor();
  // The public pure intent machine supplies the full-log oracle; no direct record publisher.
  const outcomes = intentOutcomes(records.map(record => toBinary(RecordSchema, record)));
  const expected = foldOutcomes([], outcomes);
  const reader = gatewayReader(peer, new FakeSkeletonCache(), clock.schedule);
  const failed = Promise.withResolvers<void>();
  const stop = reader.watch(
    'run-a',
    observation => {
      if (observation.failure) failed.resolve();
    },
    fanout.publish,
  );
  try {
    await waitFor(actor, s => s.context.outcomes.length === expected.length, {timeout: 2000});
    const full = actor.getSnapshot().context.outcomes;
    expect(full).toEqual(expected);
    expect(full.map(row => row.id)).toContain('intent-1');
    expect(full.map(row => row.id)).toContain('intent-2');
    expect(full.find(row => row.id === 'intent-2')?.reason).toBe('epoch closed');
    peer.fail(TAIL, new Error('disconnected'));
    await failed.promise;
    peer.recover(TAIL);
    const next = create(RecordSchema, {
      seq: BigInt(records.length + 1),
      epoch: 2,
      at: {seconds: 2000n},
      body: {
        case: 'intent',
        value: {controlId: 'after-reconnect', action: IntentAction.PARK, step: IntentStep.APPLIED},
      },
    });
    peer.publish([next]);
    clock.advanceBy(1000);
    await waitFor(actor, s => s.context.outcomes.length === expected.length + 1, {timeout: 2000});
    const reconnected = foldOutcomes(expected, intentOutcomes([toBinary(RecordSchema, next)]));
    expect(actor.getSnapshot().context.outcomes).toEqual(reconnected);
    actor.stop();
    actor = makeActor();
    expect(actor.getSnapshot().context.outcomes).toEqual(reconnected);
    return peer;
  } finally {
    stop();
    actor.stop();
    await peer.whenIdle();
  }
}

test('checkpoint, reconnect and remount retain intent and epoch history older than the record window', async () => {
  const records = log(1400);
  await exercise(records, checkpoint(records, 1400, false), 1400);
});

test('property: generated checkpoint positions and previous writers agree with the full log', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.integer({min: 6, max: 1800}),
      fc.nat(1800),
      fc.boolean(),
      async (length, position, previous) => {
        const records = log(length);
        const cut = Math.min(length, position);
        await exercise(records, checkpoint(records, cut, previous), cut);
      },
    ),
    {
      numRuns: 12,
      seed: 341810,
      examples: [
        [600, 600, false],
        [1400, 1400, true],
        [1400, 5, false],
      ],
    },
  );
});

test('the captured previous writer recovers history through the shared reader', async () => {
  const root = new URL('../../../../../proto/testdata/retained/84bf806c/', import.meta.url);
  const keyframe = readFileSync(new URL('keyframes/1121.pb', root));
  const reply = fromBinary(
    SkeletonResponseSchema,
    readFileSync(new URL('skeleton.response.pb', root)),
  );
  const records = reply.records.map(bytes => fromBinary(RecordSchema, bytes));
  // The pinned public reply is replay, so its records are the whole committed log.
  expect(records.length).toBe(1121);
  expect(fromBinary(KeyframeSchema, keyframe).recordsTracked).toBe(false);
  const peer = new CheckpointPeer(records, keyframe, records.length);
  const clock = new FakeClock();
  const fanout = recordsFanout();
  const actor = intentsActor({
    bridge: new FakeGatewayBridge(),
    host: 'local',
    runId: 'run-a',
    enabled: true,
    schedule: clock.schedule,
    records: fanout.subscribe,
  }).start();
  const stop = gatewayReader(peer, new FakeSkeletonCache(), clock.schedule).watch(
    'run-a',
    () => {},
    fanout.publish,
  );
  try {
    await waitFor(actor, s => s.context.outcomes.some(row => row.id === 'old-refusal'), {
      timeout: 2000,
    });
    expect(actor.getSnapshot().context.outcomes.map(row => row.id)).toEqual([
      'old-guidance',
      'old-refusal',
      '2',
    ]);
    const call = peer.calls.find(call => call.method === TAIL);
    if (!call) throw new Error('shared Tail did not start');
    expect(fromBinary(TailRequestSchema, call.request).fromSeq).toBe(0n);
  } finally {
    stop();
    actor.stop();
    await peer.whenIdle();
  }
});

test('an invalid checkpoint publishes no semantic facts before the reader rejects it', async () => {
  const records = log(1400);
  const peer = new CheckpointPeer(records, checkpoint(records, 1400, false), 1399);
  const clock = new FakeClock();
  const fanout = recordsFanout();
  const rejected = Promise.withResolvers<void>();
  const actor = intentsActor({
    bridge: new FakeGatewayBridge(),
    host: 'local',
    runId: 'run-a',
    enabled: true,
    schedule: clock.schedule,
    records: fanout.subscribe,
  }).start();
  const stop = gatewayReader(peer, new FakeSkeletonCache(), clock.schedule).watch(
    'run-a',
    observation => {
      if (observation.terminal && observation.failure?.kind === 'data') rejected.resolve();
    },
    fanout.publish,
  );
  try {
    await rejected.promise;
    expect(actor.getSnapshot().context.outcomes).toEqual([]);
    expect(peer.calls.filter(call => call.method === TAIL)).toEqual([]);
  } finally {
    stop();
    actor.stop();
    await peer.whenIdle();
  }
});
