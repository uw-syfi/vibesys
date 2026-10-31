import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  Change,
  DrainGoal,
  KeyframeSchema,
  LifecycleSchema,
  Outcome,
  OutcomeSchema,
  outcomePresentation,
  projectLifecycle,
  RecordSchema,
  RunHeadSchema,
  RunLiveness,
  RuntimeType,
  Severity,
  SkeletonResponseSchema,
  SpanStatus,
  type Record as StoreRecord,
  StreamKind,
  StreamState,
  TailResponseSchema,
} from '@vibesys/proto';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  epochHasEnded,
  foldSkeleton,
  ingestSkeleton,
  ingestTail,
  observeRun,
  seedSkeleton,
  seekSkeleton,
} from './index.js';

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

test('a folded state does not depend on the input records and rejects gaps, duplicates and out of range seeks', () => {
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

for (const kind of ['running', 'draining', 'parked', 'ended'] as const)
  test(`same-epoch declarations preserve ${kind} at every keyframe cut`, () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const epoch = random.int(100) + 1;
      const lifecycle = create(LifecycleSchema, {
        state:
          kind === 'ended'
            ? {case: kind, value: {outcome: Outcome.FINISHED}}
            : kind === 'draining'
              ? {case: kind, value: {goal: DrainGoal.PARK}}
              : {case: kind, value: {}},
      });
      const bodies = [
        {case: 'epochStarted', value: {runId: 'run-a', epoch}},
        {case: 'lifecycle', value: lifecycle},
        ...Array.from(
          {length: random.int(5) + 1},
          () =>
            ({
              case: random.pick(['epochStarted', 'declarationAmended'] as const),
              value: {runId: 'run-a', epoch},
            }) as const,
        ),
      ] as const;
      const records = bodies.map((body, index) =>
        create(RecordSchema, {
          seq: BigInt(index + 1),
          epoch,
          body,
        }),
      );
      for (let cut = 0; cut <= records.length; cut++) {
        const prefix = foldSkeleton(seedSkeleton(), records.slice(0, cut));
        const cached =
          cut === 0
            ? prefix
            : seedSkeleton(
                create(KeyframeSchema, {
                  ...prefix,
                  throughSeq: prefix.throughSeq ?? 0n,
                  logOffset: 1n,
                }),
              );
        const full = foldSkeleton(cached, records.slice(cut));
        const observed = observeRun(full, {epoch, value: RunLiveness.INTERRUPTED});
        expect(observed.lifecycle).toEqual(projectLifecycle(lifecycle));
        expect(epochHasEnded(lifecycle)).toBe(kind === 'parked' || kind === 'ended');
        expect(observed.final).toBe(epochHasEnded(lifecycle));
        const successor = foldSkeleton(full, [
          create(RecordSchema, {
            seq: BigInt(records.length + 1),
            epoch: epoch + 1,
            body: {case: 'epochStarted', value: {runId: 'run-a', epoch: epoch + 1}},
          }),
        ]);
        expect(observeRun(successor).lifecycle).toBeUndefined();
      }
    }
  });

test('the fold keeps the latest error notice of each epoch, wherever the notices sit', () => {
  let state = 7;
  const next = (bound: number) => {
    state = (state * 1103515245 + 12345) % 2 ** 31;
    return state % bound;
  };
  for (let round = 0; round < 12; round += 1) {
    const records = Array.from({length: next(150)}, (_, index) =>
      create(RecordSchema, {
        seq: BigInt(index + 1),
        epoch: 1 + Math.floor((index * 3) / 150),
        body: {
          case: 'notice',
          value: {severity: [Severity.WARNING, Severity.ERROR, Severity.FATAL][next(3)] ?? 1},
        },
      }),
    );
    const folded = foldSkeleton(seedSkeleton(), records);
    const latest = new Map<number, bigint>();
    for (const record of records)
      if (record.body.case === 'notice' && record.body.value.severity >= Severity.ERROR)
        latest.set(record.epoch, record.seq);
    expect(new Map(folded.failures.map(record => [record.epoch, record.seq]))).toEqual(latest);
  }
});

const failure = (seq: bigint, epoch: number, severity: Severity) =>
  create(RecordSchema, {seq, epoch, body: {case: 'notice', value: {severity}}});
const keyframeOf = (failures: ReturnType<typeof failure>[], failuresTracked = true) =>
  create(KeyframeSchema, {throughSeq: 305n, logOffset: 8n, failures, failuresTracked});

test('a keyframe whose failures are not error notices of its prefix is refused', () => {
  for (const bad of [
    keyframeOf([failure(9999n, 1, Severity.ERROR)]),
    keyframeOf([failure(4n, 1, Severity.WARNING)]),
    keyframeOf([failure(4n, 1, Severity.ERROR), failure(5n, 1, Severity.FATAL)]),
    keyframeOf([failure(4n, 1, Severity.ERROR)], false),
  ])
    expect(() => seedSkeleton(bad)).toThrow('keyframe.failures');
  expect(
    seedSkeleton(keyframeOf([failure(4n, 1, Severity.ERROR), failure(300n, 2, Severity.FATAL)]))
      .failures,
  ).toHaveLength(2);
  expect(seedSkeleton(keyframeOf([], false)).failuresTracked).toBe(false);
});

test('an ended run reads the presentation of its outcome from the records and from the head alike', () => {
  const outcomes = OutcomeSchema.values.map(value => value.number).filter(number => number !== 0);
  expect(outcomes.length).toBeGreaterThan(0);
  for (const outcome of outcomes) {
    const lifecycle = create(LifecycleSchema, {state: {case: 'ended', value: {outcome}}});
    const bodies = [
      {case: 'epochStarted', value: {runId: 'run-a', epoch: 1}},
      {case: 'lifecycle', value: lifecycle},
    ] as const;
    const records = bodies.map((body, index) =>
      create(RecordSchema, {seq: BigInt(index + 1), epoch: 1, body}),
    );
    const folded = observeRun(foldSkeleton(seedSkeleton(), records));
    const head = create(RunHeadSchema, {
      epoch: 1,
      committedSeq: 2n,
      lifecycle: projectLifecycle(lifecycle),
    });
    const fromHead = observeRun(foldSkeleton(seedSkeleton(), records), {
      epoch: 1,
      value: RunLiveness.ALIVE,
      head,
    });
    expect(folded.lifecycle).toEqual(fromHead.lifecycle);
    expect(
      folded.lifecycle?.state.case === 'ended' && folded.lifecycle.state.value.presentation,
    ).toEqual(outcomePresentation(outcome));
  }
});
