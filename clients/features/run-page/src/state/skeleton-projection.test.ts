import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {
  AttemptOutcome,
  Change,
  DrainGoal,
  KeyframeSchema,
  Outcome,
  RecordSchema,
  RuntimeType,
  SpanFailureKind,
  SpanSchema,
  SpanStatus,
  StageState,
  StartupStage,
  WaitKind,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {
  headerView,
  projectSkeleton,
  runPageInitial,
  skeletonPositions,
  skeletonScreenState,
  timelineView,
} from './index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

for (let seed = 1; seed <= 24; seed += 1) {
  test(`property: seek and keyframes match every prefix (seed ${seed})`, () => {
    const {records} = storeRunFixture(
      seed % 3 === 0 ? 'blobs' : seed % 2 ? 'short' : 'live',
      1 + (seed % 5),
      seed,
    );
    const full = foldSkeleton(seedSkeleton(), records);
    const cached = seedSkeleton(
      create(KeyframeSchema, {...full, throughSeq: full.throughSeq ?? 0n, logOffset: 8n}),
    );
    for (let length = 0; length <= records.length; length += 1) {
      const prefix = foldSkeleton(seedSkeleton(), records.slice(0, length));
      const seq = prefix.throughSeq ?? 0n;
      expect(projectSkeleton(full, seq)).toEqual(projectSkeleton(prefix, seq));
      expect(projectSkeleton(cached, seq)).toEqual(projectSkeleton(prefix, seq));
    }
    expect(skeletonPositions(full)).toEqual(
      [
        ...new Set([
          0,
          ...records
            .filter(record => record.body.case !== 'stream')
            .map(record => Number(record.seq)),
          Number(full.throughSeq),
        ]),
      ].sort((a, b) => a - b),
    );
  });
}

test('reported metrics and usage reach the shared screen; missing figures remain unknown', () => {
  const {records} = storeRunFixture('short', 2, 3);
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, 'Throughput');
  expect(model.overview?.objective.text).toBe('Throughput');
  expect(model.candidates.candidates[0]?.measurement?.metricValue).toBe(103);
  expect(model.candidates.candidates[0]?.measurement?.vsBaseline).toBeNull();
  const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
  expect(state.run.core?.tokens.total.input).toBe(26);
  expect(state.run.core?.tokens.total.cacheRead).toBeNull();
  expect(headerView(state).status.label).toBe('Finished');
  expect(state.run.core?.usageRecords).toHaveLength(2);
});

test('descriptor and sequence errors are explicit; projection owns no input', () => {
  const {records} = storeRunFixture();
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const before = seekSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  expect(seekSkeleton(skeleton, skeleton.throughSeq ?? 0n)).toEqual(before);
  expect(() => projectSkeleton(skeleton, (skeleton.throughSeq ?? 0n) + 1n)).toThrow();
  const span = Object.values(skeleton.spans)[0]?.versions[0]?.span;
  if (!span?.label?.attrs) throw new Error('fixture span required');
  span.label.attrs.typeUrl = 'type.example/unknown.Attrs';
  expect(() => projectSkeleton(skeleton, 3n)).toThrow('undeclared attrs');
  span.status = SpanStatus.UNSPECIFIED;
  expect(() => projectSkeleton(skeleton, 3n)).toThrow('invalid status');
});

test('comparison links, adoption, baselines and capacity spans use the existing model', () => {
  const {records} = storeRunFixture('facts');
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  expect(model.candidates.candidates[0]?.measurement?.metricValue).toBe(125);
  expect(model.candidates.candidates[0]?.measurement?.vsBaseline).toBe(1.25);
  expect(model.candidates.candidates[0]?.measurement?.against?.baseCommit).toBe('input');
  expect(model.candidates.best).toEqual({candidate: 'idea-0', adopted: true});
  expect(model.baselines[0]?.value).toBe(50);
  expect(model.pauses).toEqual([
    {from: 0, to: 2, provider: 'provider-a', condition: 'rate_limited'},
  ]);
  for (const record of records)
    expect(projectSkeleton(skeleton, record.seq)).toEqual(
      projectSkeleton(
        foldSkeleton(
          seedSkeleton(),
          records.filter(item => item.seq <= record.seq),
        ),
        record.seq,
      ),
    );
});

test('startup reports only recorded stages and the gateway never offers legacy commands', () => {
  const initial = storeRunFixture('live', 1).records.slice(0, 1);
  const records = [
    ...initial,
    create(RecordSchema, {
      seq: 2n,
      epoch: 1,
      at: {seconds: 1700000001n},
      body: {
        case: 'lifecycle',
        value: {
          state: {
            case: 'starting',
            value: {stage: StartupStage.BUILDING_TOOLS, state: StageState.RUNNING},
          },
        },
      },
    }),
  ];
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const model = projectSkeleton(skeleton, 2n);
  const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
  expect(timelineView(state)).toEqual([
    {id: 'building_tools', label: 'Tools', state: 'active', streamId: null},
  ]);
  expect(headerView(state).pauseNow.shown).toBe(false);
  expect(headerView(state).pause.shown).toBe(false);
  expect(headerView(state).status.label).toBe('Starting');
});

test('park, drain and interruption keep their status words; past logs have no invented offsets', () => {
  const fixture = storeRunFixture('short');
  for (const [state, word] of [
    [{case: 'parked', value: {}}, 'Paused'],
    [{case: 'draining', value: {goal: DrainGoal.PARK}}, 'Pausing'],
    [{case: 'ended', value: {outcome: Outcome.INTERRUPTED}}, 'Interrupted'],
  ] as const) {
    const records = [
      ...fixture.records,
      create(RecordSchema, {
        seq: BigInt(fixture.records.length + 1),
        epoch: 1,
        at: {seconds: 1700001000n},
        body: {case: 'lifecycle', value: {state}},
      }),
    ];
    const skeleton = foldSkeleton(seedSkeleton(), records);
    const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
    const current = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
    expect(headerView(current).status.label).toBe(word);
    const past = skeletonScreenState(SEED, skeleton, model, {
      ...runPageInitial(SEED),
      view: {at: 3},
    });
    expect(past.logs.kind === 'listed' ? past.logs.streams : undefined).toEqual([]);
  }
});

function genericSpan(status: SpanStatus, wait: WaitKind, outcome: AttemptOutcome) {
  return create(SpanSchema, {
    id: 'span-a',
    runtimeType: RuntimeType.OPERATION,
    name: 'Work',
    status,
    startedAt: 0,
    endedAt: status >= SpanStatus.DONE ? 1 : undefined,
    waitingOn: [{kind: wait, ref: 'wait-a', since: 0}],
    attempts: [{n: 1, of: 3, startedAt: 0, endedAt: 1, outcome, reason: 'Retry'}],
    failure:
      status === SpanStatus.FAILED ? {kind: SpanFailureKind.OUTCOME, message: 'Failed'} : undefined,
    stop: status === SpanStatus.STOPPED ? {requestedAt: 0.5, landed: true} : undefined,
  });
}
for (const [status, word] of [
  [SpanStatus.RUNNING, 'running'],
  [SpanStatus.WAITING, 'waiting'],
  [SpanStatus.PENDING, 'pending'],
  [SpanStatus.DONE, 'done'],
  [SpanStatus.FAILED, 'failed'],
  [SpanStatus.STOPPED, 'stopped'],
] as const) {
  test(`generated generic ${word} spans preserve waits, attempts and detail`, () => {
    const epoch = storeRunFixture().records[0];
    if (!epoch) throw new Error('epoch required');
    for (const wait of [WaitKind.OPERATION, WaitKind.SLOT, WaitKind.TIME, WaitKind.STOP])
      for (const outcome of [
        AttemptOutcome.DONE,
        AttemptOutcome.AGAIN,
        AttemptOutcome.GAVE_UP,
      ] as const) {
        const span = genericSpan(status, wait, outcome);
        const skeleton = foldSkeleton(seedSkeleton(), [
          epoch,
          create(RecordSchema, {
            seq: 2n,
            epoch: 1,
            at: {seconds: 1700000001n},
            body: {case: 'span', value: {change: Change.UPDATED, span}},
          }),
        ]);
        const projected = projectSkeleton(skeleton, 2n).trace.spans.get('span-a');
        expect(projected?.status).toBe(word);
        expect(projected?.waiting_on?.[0]?.ref).toBe('wait-a');
        expect(projected?.attempts?.[0]?.n).toBe(1);
        expect(projected?.attempts?.[0]?.outcome).toBe(
          (
            {
              [AttemptOutcome.DONE]: 'done',
              [AttemptOutcome.AGAIN]: 'again',
              [AttemptOutcome.GAVE_UP]: 'gave_up',
            } as const
          )[outcome],
        );
        expect(projected?.stop?.landed ?? false).toBe(status === SpanStatus.STOPPED);
        expect(projected?.failure?.message ?? null).toBe(
          status === SpanStatus.FAILED ? 'Failed' : null,
        );
      }
  });
}

test('the new scrubber reports monotone elapsed time without fabricating a wall-clock stamp', () => {
  const epoch = storeRunFixture().records[0];
  if (!epoch) throw new Error('epoch required');
  const skeleton = foldSkeleton(seedSkeleton(), [
    epoch,
    create(RecordSchema, {
      seq: 2n,
      epoch: 1,
      at: {seconds: 1700000001n},
      body: {case: 'metric', value: {series: 'throughput', value: 100, clock: 90061}},
    }),
  ]);
  const model = projectSkeleton(skeleton, 2n);
  expect(model.latest).toBe(90061);
  const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
  expect(state.positionWord).toBe('25h 01m');
});
