import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {checkMachine} from '@vibesys/machine/testing';
import {
  RecordSchema,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {type AbortSignalLike, registerAbort} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import type {Actor, SnapshotFrom} from 'xstate';
import {type RunTransport, skeletonLoaderMachine, startSkeletonLoader} from './index.js';

const INPUT = {runId: 'run-a', timeoutMs: 50};
const initial = () => create(SkeletonResponseSchema, {committedSeq: 2n, nextSeq: 1n});
const batch = (seq: bigint) =>
  create(TailResponseSchema, {
    throughSeq: seq,
    records: [
      toBinary(
        RecordSchema,
        create(RecordSchema, {
          seq,
          body: {case: 'lifecycle', value: {state: {case: 'running', value: {}}}},
        }),
      ),
    ],
  });

test('model: requests and deadlines belong to active states, errors always release work', () => {
  checkMachine({
    machine: skeletonLoaderMachine,
    input: INPUT,
    event: (random, live, snapshot) => {
      if (snapshot.status === 'done') return null;
      return random.pick([
        ...live.operations.flatMap(operation => [
          live.done(operation, operation === 'skeleton' ? initial() : undefined),
          live.error(operation, new Error('transport')),
        ]),
        ...live.delays.map(delay => live.fire(delay)),
        {type: 'tail', response: batch(snapshot.context.load?.nextSeq ?? 1n)},
        {type: 'failed', error: new Error('decode')},
      ]);
    },
    invariants: snapshot =>
      snapshot.matches({following: 'live'}) && !snapshot.context.load?.complete
        ? ['live before committed prefix']
        : [],
  });
});

/** A controlled byte bridge; requests are decoded and stream lifetime honors the owner's signal. */
class FakeRunTransport implements RunTransport {
  unarySignal: AbortSignalLike | undefined;
  streamSignal: AbortSignalLike | undefined;
  fromSeq: bigint | undefined;
  finishSkeleton: (response: Uint8Array) => void = () => {
    throw new Error('no unary');
  };
  failSkeleton: (error: Error) => void = () => {
    throw new Error('no unary');
  };
  push: (response: Uint8Array) => void = () => {
    throw new Error('no stream');
  };
  endStream: () => void = () => {
    throw new Error('no stream');
  };
  streamClosed = false;

  unary(method: string, request: Uint8Array, signal: AbortSignalLike): Promise<Uint8Array> {
    expect(method).toBe('/vibesys.api.v1.HostService/Skeleton');
    expect(fromBinary(SkeletonRequestSchema, request).runId).toBe(INPUT.runId);
    this.unarySignal = signal;
    return new Promise((resolve, reject) => {
      this.finishSkeleton = resolve;
      this.failSkeleton = reject;
    });
  }
  stream(method: string, request: Uint8Array, signal: AbortSignalLike): AsyncIterable<Uint8Array> {
    expect(method).toBe('/vibesys.api.v1.HostService/Tail');
    const decoded = fromBinary(TailRequestSchema, request);
    expect(decoded.runId).toBe(INPUT.runId);
    this.fromSeq = decoded.fromSeq;
    this.streamSignal = signal;
    return {
      [Symbol.asyncIterator]: () => ({
        next: () =>
          new Promise<IteratorResult<Uint8Array>>(resolve => {
            const abort = () => {
              this.streamClosed = true;
              resolve({done: true, value: undefined});
            };
            this.endStream = () => {
              registration.detach();
              this.streamClosed = true;
              resolve({done: true, value: undefined});
            };
            const registration = registerAbort(signal, abort);
            if (registration.aborted) abort();
            this.push = value => {
              registration.detach();
              resolve({done: false, value});
            };
          }),
        return: async () => {
          this.streamClosed = true;
          return {done: true, value: undefined};
        },
      }),
    };
  }
}

function reached(
  actor: Actor<typeof skeletonLoaderMachine>,
  predicate: (snapshot: SnapshotFrom<typeof skeletonLoaderMachine>) => boolean,
): Promise<void> {
  if (predicate(actor.getSnapshot())) return Promise.resolve();
  return new Promise(resolve => {
    const subscription = actor.subscribe(snapshot => {
      if (predicate(snapshot)) {
        subscription.unsubscribe();
        resolve();
      }
    });
  });
}

test('live byte bridge starts at next_seq, catches up, folds live batches and closes on stop', async () => {
  const transport = new FakeRunTransport();
  const clock = new FakeClock();
  const actor = startSkeletonLoader(transport, clock.schedule, INPUT);
  transport.finishSkeleton(toBinary(SkeletonResponseSchema, initial()));
  await reached(actor, snapshot => snapshot.matches({following: 'catchingUp'}));
  expect(transport.fromSeq).toBe(1n);
  transport.push(toBinary(TailResponseSchema, batch(1n)));
  await reached(actor, snapshot => snapshot.context.load?.nextSeq === 2n);
  expect(actor.getSnapshot().matches({following: 'catchingUp'})).toBe(true);
  transport.push(toBinary(TailResponseSchema, batch(2n)));
  await reached(actor, snapshot => snapshot.matches({following: 'live'}));
  expect(clock.pending).toBe(0);
  transport.push(toBinary(TailResponseSchema, batch(3n)));
  await reached(actor, snapshot => snapshot.context.load?.nextSeq === 4n);
  actor.stop();
  expect(transport.streamSignal?.aborted).toBe(true);
  expect(transport.streamClosed).toBe(true);
});

test('unary timeout aborts ownership and ignores a late response', async () => {
  const transport = new FakeRunTransport();
  const clock = new FakeClock();
  const actor = startSkeletonLoader(transport, clock.schedule, INPUT);
  clock.advanceBy(INPUT.timeoutMs);
  expect(actor.getSnapshot().matches('recovering')).toBe(true);
  expect(transport.unarySignal?.aborted).toBe(true);
  transport.finishSkeleton(toBinary(SkeletonResponseSchema, initial()));
  await Promise.resolve();
  expect(actor.getSnapshot().context.load).toBeUndefined();
  expect(transport.streamSignal).toBeUndefined();
  expect(clock.pending).toBe(1);
  actor.stop();
  expect(clock.pending).toBe(0);
});

test.each([
  'deadline',
  'wire',
  'gap',
  'unary',
  'skeleton',
] as const)('failure %s releases requests and deadlines', async failure => {
  const transport = new FakeRunTransport();
  const clock = new FakeClock();
  const actor = startSkeletonLoader(transport, clock.schedule, INPUT);
  if (failure === 'unary') transport.failSkeleton(new Error('dial failed'));
  else if (failure === 'skeleton')
    transport.finishSkeleton(
      toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {complete: true, committedSeq: 5n}),
      ),
    );
  else {
    transport.finishSkeleton(toBinary(SkeletonResponseSchema, initial()));
    await reached(actor, snapshot => snapshot.matches('following'));
    if (failure === 'deadline') clock.advanceBy(INPUT.timeoutMs);
    else
      transport.push(
        failure === 'wire' ? new Uint8Array([255]) : toBinary(TailResponseSchema, batch(2n)),
      );
  }
  const retries = failure === 'deadline' || failure === 'unary' || failure === 'wire';
  await reached(actor, snapshot => snapshot.matches(retries ? 'recovering' : 'failed'));
  expect(actor.getSnapshot().context.error).toBeDefined();
  expect(clock.pending).toBe(retries ? 1 : 0);
  actor.stop();
  expect(clock.pending).toBe(0);
  if (transport.streamSignal)
    expect(transport.streamSignal.aborted || transport.streamClosed).toBe(true);
});

test('closed streams recover from a fresh Skeleton and release retries when stopped', async () => {
  const transport = new FakeRunTransport();
  const clock = new FakeClock();
  const actor = startSkeletonLoader(transport, clock.schedule, INPUT);
  for (let cycle = 0; cycle < 5; cycle += 1) {
    transport.finishSkeleton(toBinary(SkeletonResponseSchema, initial()));
    await reached(actor, snapshot => snapshot.matches('following'));
    transport.push(toBinary(TailResponseSchema, batch(1n)));
    await reached(actor, snapshot => snapshot.context.load?.nextSeq === 2n);
    transport.push(toBinary(TailResponseSchema, batch(2n)));
    await reached(actor, snapshot => snapshot.matches({following: 'live'}));
    expect(actor.getSnapshot().context.error).toBeUndefined();
    transport.endStream();
    await reached(actor, snapshot => snapshot.matches('recovering'));
    expect(actor.getSnapshot().context.load?.nextSeq).toBe(3n);
    expect(actor.getSnapshot().context.error?.kind).toBe('stream-ended');
    expect(clock.pending).toBe(1);
    clock.advanceBy(1000);
    expect(actor.getSnapshot().matches('loading')).toBe(true);
  }
  actor.stop();
  expect(clock.pending).toBe(0);
  expect(transport.unarySignal?.aborted).toBe(true);
});
