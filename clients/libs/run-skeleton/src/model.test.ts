import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  Change,
  DrainGoal,
  KeyframeSchema,
  LifecycleSchema,
  Outcome,
  RecordSchema,
  RuntimeType,
  SkeletonResponseSchema,
  SpanStatus,
  type Record as StoreRecord,
  StreamKind,
  StreamState,
  TailResponseSchema,
} from '@vibesys/proto';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {foldSkeleton, ingestSkeleton, ingestTail, seedSkeleton, seekSkeleton} from './index.js';

function recordsFor(seed: number): StoreRecord[] {
  const random = new Seeded(seed);
  return Array.from({length: 32}, (_, index) => {
    const seq = BigInt(index + 1);
    const record = create(RecordSchema, {seq, epoch: 1, at: {seconds: seq}});
    const status = random.pick([
      SpanStatus.RUNNING,
      SpanStatus.WAITING,
      SpanStatus.DONE,
      SpanStatus.FAILED,
      SpanStatus.STOPPED,
    ]);
    const lifecycle = random.pick([
      create(LifecycleSchema, {state: {case: 'running', value: {}}}),
      create(LifecycleSchema, {state: {case: 'parked', value: {}}}),
      create(LifecycleSchema, {state: {case: 'draining', value: {goal: DrainGoal.PARK}}}),
      create(LifecycleSchema, {
        state: {case: 'ended', value: {outcome: Outcome.FINISHED, summary: `ended-${index}`}},
      }),
    ]);
    const bodies = [
      {
        case: 'span',
        value: {
          change: Change.UPDATED,
          span: {
            id: random.pick(['a', 'b', '__proto__', 'constructor']),
            name: `span-${index}`,
            runtimeType: RuntimeType.OPERATION,
            status,
            startedAt: index,
            endedAt: status >= SpanStatus.DONE ? index + 1 : undefined,
          },
        },
      },
      {
        case: 'metric',
        value: {series: random.pick(['cost', 'score']), value: random.int(200) - 100, clock: index},
      },
      {case: 'lifecycle', value: lifecycle},
      {case: 'epochStarted', value: {runId: 'run-a', epoch: index + 1}},
      {
        case: 'stream',
        value: {
          id: random.pick(['log-a', 'log-b']),
          kind: StreamKind.LOG,
          title: `log-${index}`,
          state: StreamState.OPENED,
        },
      },
      {case: 'legacyEvent', value: {json: '{}'}},
      {case: undefined},
    ] as const;
    record.body = create(RecordSchema, {body: random.pick(bodies)}).body;
    // Exercise the generated encoder/decoder, including absent (future) oneof bodies.
    return fromBinary(RecordSchema, toBinary(RecordSchema, record));
  });
}

test('every keyframe cut reconstructs the full skeleton and every seek equals a prefix fold', () => {
  for (const seed of SEEDS) {
    const records = recordsFor(seed);
    const full = foldSkeleton(seedSkeleton(), records);
    for (let k = 0; k <= records.length; k++) {
      const prefix = foldSkeleton(seedSkeleton(), records.slice(0, k));
      const keyframe = fromBinary(
        KeyframeSchema,
        toBinary(
          KeyframeSchema,
          create(KeyframeSchema, {
            ...prefix,
            throughSeq: prefix.throughSeq ?? 0n,
            logOffset: BigInt(k + 8),
          }),
        ),
      );
      const resumed = foldSkeleton(
        k === 0 ? seedSkeleton() : seedSkeleton(keyframe),
        records.slice(k),
      );
      expect(resumed).toEqual(full);
      const expected = seekSkeleton(prefix, BigInt(k));
      expect(seekSkeleton(full, BigInt(k))).toEqual(expected);
      expect(seekSkeleton(resumed, BigInt(k))).toEqual(expected);
    }
  }
});

test('fold owns its inputs and rejects gaps, duplicates and out of range seeks', () => {
  const records = recordsFor(9);
  const initial = seedSkeleton();
  const state = foldSkeleton(initial, records);
  const snapshot = toBinary(
    KeyframeSchema,
    create(KeyframeSchema, {...state, throughSeq: state.throughSeq ?? 0n}),
  );
  for (const record of records) record.body = {case: undefined};
  expect(
    toBinary(
      KeyframeSchema,
      create(KeyframeSchema, {...state, throughSeq: state.throughSeq ?? 0n}),
    ),
  ).toEqual(snapshot);
  expect(initial.throughSeq).toBeUndefined();
  expect(() => foldSkeleton(state, [create(RecordSchema, {seq: 32n})])).toThrow('record.seq');
  expect(() => foldSkeleton(state, [create(RecordSchema, {seq: 34n})])).toThrow('record.seq');
  expect(() => seekSkeleton(state, -1n)).toThrow('seek.seq');
  expect(() => seekSkeleton(state, 33n)).toThrow('seek.seq');
});

test('bounded Skeleton prefixes continue with the identical Tail fold into live records', () => {
  const records = recordsFor(17);
  for (let cut = 0; cut < records.length; cut++) {
    const initial = ingestSkeleton(
      create(SkeletonResponseSchema, {
        records: records.slice(0, cut).map(record => toBinary(RecordSchema, record)),
        committedSeq: 31n,
        nextSeq: BigInt(cut + 1),
        complete: cut === 31,
      }),
    );
    const caughtUp = ingestTail(
      initial,
      create(TailResponseSchema, {
        records: records.slice(cut, 31).map(record => toBinary(RecordSchema, record)),
        throughSeq: 31n,
      }),
    );
    expect(caughtUp.complete).toBe(true);
    const live = ingestTail(
      caughtUp,
      create(TailResponseSchema, {
        records: [toBinary(RecordSchema, records.at(-1) as StoreRecord)],
        throughSeq: 32n,
      }),
    );
    expect(live.skeleton).toEqual(foldSkeleton(seedSkeleton(), records));
    expect(live.nextSeq).toBe(33n);
  }
});

test('ingestion rejects contradictory boundaries and malformed protobuf', () => {
  expect(() =>
    ingestSkeleton(
      create(SkeletonResponseSchema, {
        nextSeq: 1n,
        records: [toBinary(RecordSchema, create(RecordSchema, {seq: 1n}))],
        committedSeq: 1n,
        complete: true,
      }),
    ),
  ).toThrow('next_seq');
  expect(() =>
    ingestSkeleton(create(SkeletonResponseSchema, {nextSeq: 1n, committedSeq: 2n, complete: true})),
  ).toThrow('complete');
  expect(() =>
    ingestSkeleton(create(SkeletonResponseSchema, {keyframe: new Uint8Array([255])})),
  ).toThrow();
  const state = ingestSkeleton(create(SkeletonResponseSchema, {nextSeq: 1n, complete: true}));
  expect(() => ingestTail(state, create(TailResponseSchema, {throughSeq: 1n}))).toThrow(
    'through_seq',
  );
});

test('phase 1 zero sequences, gaps and truncated boundaries use the server next_seq', () => {
  const legacy = (seq: bigint) =>
    toBinary(
      RecordSchema,
      create(RecordSchema, {seq, body: {case: 'legacyEvent', value: {json: '{}'}}}),
    );
  const empty = ingestSkeleton(create(SkeletonResponseSchema, {nextSeq: 0n, complete: true}));
  expect(empty.skeleton.throughSeq).toBeUndefined();
  expect(empty.nextSeq).toBe(0n);
  const omitted = ingestSkeleton(
    create(SkeletonResponseSchema, {committedSeq: 0n, nextSeq: 0n, complete: false}),
  );
  const zero = ingestTail(
    omitted,
    create(TailResponseSchema, {records: [legacy(0n)], throughSeq: 0n}),
  );
  expect(zero.complete).toBe(true);
  const prefix = ingestSkeleton(
    create(SkeletonResponseSchema, {
      records: [legacy(0n)],
      committedSeq: 0n,
      nextSeq: 3n,
      complete: false,
    }),
  );
  expect(prefix.complete).toBe(false);
  const resumed = ingestTail(
    prefix,
    create(TailResponseSchema, {records: [legacy(3n), legacy(6n)], throughSeq: 6n}),
  );
  expect(resumed.complete).toBe(true);
  expect(resumed.nextSeq).toBe(7n);
  expect(() =>
    ingestTail(prefix, create(TailResponseSchema, {records: [legacy(1n)], throughSeq: 1n})),
  ).toThrow('next_seq');
  expect(() => foldSkeleton(zero.skeleton, [fromBinary(RecordSchema, legacy(0n))])).toThrow(
    'record.seq',
  );
});

test('keyframe ingestion preserves history and rejects invalid indexes before seeking', () => {
  const records = recordsFor(20);
  const prefix = foldSkeleton(seedSkeleton(), records.slice(0, 12));
  const keyframe = create(KeyframeSchema, {...prefix, throughSeq: 12n, logOffset: 90n});
  const loaded = ingestSkeleton(
    create(SkeletonResponseSchema, {
      keyframe: toBinary(KeyframeSchema, keyframe),
      records: records.slice(12).map(record => toBinary(RecordSchema, record)),
      committedSeq: 32n,
      nextSeq: 33n,
      complete: true,
    }),
  );
  expect(loaded.skeleton).toEqual(foldSkeleton(seedSkeleton(), records));
  const invalid = create(KeyframeSchema, {
    throughSeq: 2n,
    spans: {
      a: {
        versions: [
          {seq: 2n, span: {id: 'a'}},
          {seq: 1n, span: {id: 'a'}},
        ],
      },
    },
  });
  expect(() => seedSkeleton(invalid)).toThrow('seq');
});
