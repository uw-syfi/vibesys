import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {AnySchema} from '@bufbuild/protobuf/wkt';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {recordedProjectRoot} from '@vibesys/hosts';
import {
  AgentTurnAttrsSchema,
  AttemptOutcome,
  Change,
  ClientReplaySchema,
  Direction,
  DrainGoal,
  KeyframeSchema,
  LifecycleSchema,
  MetricSeriesDeclSchema,
  ObjectiveSchema,
  Outcome,
  RecordSchema,
  RunHeadSchema,
  RunLiveness,
  RuntimeType,
  SkeletonResponseSchema,
  SpanFailureKind,
  SpanHistorySchema,
  SpanSchema,
  SpanStatus,
  StageState,
  StartupStage,
  TokenUsageSchema,
  WaitKind,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, ingestSkeleton, seedSkeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {readSkeleton} from '@vibesys/run-state/skeleton';
import {totalUsage, usageOverTime} from '@vibesys/run-state/usage';
import {firstChart} from './chart-fixtures.js';
import {
  clockStart,
  headerView,
  heroView,
  projectSkeleton,
  recordedRunRoot,
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
  expect(model.candidates.best).toBeNull(); // No core decision was supplied with this prefix.
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
  for (const [stage, id, label] of [
    [StartupStage.PROBING_WORKSPACE, 'probing_workspace', 'Probing the workspace'],
    [StartupStage.BUILDING_IMAGE, 'building_image', 'Building the agent image'],
    [StartupStage.BUILDING_TOOLS, 'building_tools', 'Building the agent tools'],
    [StartupStage.STARTING_CONTAINER, 'starting_container', 'Starting the agent container'],
  ] as const) {
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
              value: {stage, state: StageState.RUNNING},
            },
          },
        },
      }),
    ];
    const skeleton = foldSkeleton(seedSkeleton(), records);
    const model = projectSkeleton(skeleton, 2n);
    const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
    expect(timelineView(state)).toEqual([{id, label, state: 'active', streamId: null}]);
    expect(headerView(state).pauseNow.shown).toBe(false);
    expect(headerView(state).pause.shown).toBe(false);
    expect(headerView(state).status.label).toBe('Starting');
  }
});

test('park, drain and interruption keep their status words; past logs have no invented offsets', () => {
  const fixture = storeRunFixture('short');
  for (const [state, word] of [
    [{case: 'parked', value: {}}, 'Paused'],
    [{case: 'draining', value: {goal: DrainGoal.PARK}}, 'Pausing'],
    [{case: 'ended', value: {outcome: Outcome.INTERRUPTED}}, 'Stopped'],
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

test('usage retains recorded wall timestamps across elapsed-clock gaps and unknown values', () => {
  for (const seed of [0, 1, 10, 1000]) {
    const fixture = storeRunFixture('short', 1);
    const records = fixture.records.map(record => {
      const copied = clone(RecordSchema, record);
      if (copied.body.case !== 'span') return copied;
      const span = copied.body.value.span;
      if (span?.label?.kind !== 'agent_turn') return copied;
      span.startedAt = 1;
      span.endedAt = 4;
      span.label.attrs = create(AnySchema, {
        typeUrl: `type.googleapis.com/${AgentTurnAttrsSchema.typeName}`,
        value: toBinary(
          AgentTurnAttrsSchema,
          create(AgentTurnAttrsSchema, {
            agent: 'agent-a',
            role: 'implementer',
            stage: 'implement',
            model: 'model-a',
            startedAt: {seconds: 1700000000n + BigInt(seed)},
            ...(seed === 0 ? {} : {endedAt: {seconds: 1700000003n + BigInt(seed)}}),
            usage: {input: 3n},
          }),
        ),
      });
      return copied;
    });
    const skeleton = foldSkeleton(seedSkeleton(), records);
    const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
    const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
    const usage = state.run.core?.usageRecords[0];
    expect(usage?.started_at).toBe(new Date((1700000000 + seed) * 1000).toISOString());
    expect(usage?.ended_at).toBe(
      seed === 0 ? null : new Date((1700000003 + seed) * 1000).toISOString(),
    );
    expect(usage?.model).toBe('model-a');
    const chart = usageOverTime(state.run.core?.usageRecords ?? [], 'model', 4);
    expect(chart.undated.turns).toBe(seed === 0 ? 1 : 0);
    expect(chart.buckets.length > 0).toBe(seed !== 0);
  }
});

test('a keyframe map order cannot change the run origin or performance chart duration', () => {
  for (let seed = 1; seed <= 12; seed += 1) {
    const fixture = storeRunFixture('short', 1 + (seed % 4), seed);
    const skeleton = foldSkeleton(seedSkeleton(), fixture.records);
    const through = skeleton.throughSeq ?? 0n;
    const fromRecords = projectSkeleton(skeleton, through);
    const entries = Object.entries(skeleton.spans);
    for (let offset = 1; offset < entries.length; offset += 1) {
      const reordered = [...entries.slice(offset), ...entries.slice(0, offset)];
      const keyframe = seedSkeleton(
        create(KeyframeSchema, {
          ...skeleton,
          throughSeq: through,
          logOffset: 8n,
          spans: Object.fromEntries(reordered),
        }),
      );
      const fromKeyframe = projectSkeleton(keyframe, through);
      expect(clockStart(fromKeyframe)).toBe(clockStart(fromRecords));
      expect(firstChart(fromKeyframe, clockStart(fromKeyframe), fromKeyframe.latest)).toEqual(
        firstChart(fromRecords, clockStart(fromRecords), fromRecords.latest),
      );
    }
  }
});

const HINT_STATES = [
  {case: 'running', value: {}},
  {case: 'running', value: {hold: {reason: {case: 'operator', value: {}}}}},
  {case: 'parked', value: {}},
  {case: 'ended', value: {outcome: Outcome.FINISHED}},
  {case: 'ended', value: {outcome: Outcome.INTERRUPTED}},
] as const;

for (let epoch = 1; epoch <= 8; epoch += 1)
  test.each([
    ...HINT_STATES,
  ])(`property: epoch ${epoch} lock hints affect only open current epochs: %p`, state => {
    const first = storeRunFixture('live').records[0];
    if (first?.body.case !== 'epochStarted') throw new Error('epoch required');
    const started = create(RecordSchema, {
      ...first,
      epoch,
      body: {case: 'epochStarted', value: {...first.body.value, epoch}},
    });
    const skeleton = foldSkeleton(seedSkeleton(), [
      started,
      create(RecordSchema, {
        seq: started.seq + 1n,
        epoch,
        body: {case: 'lifecycle', value: {state}},
      }),
    ]);
    const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
    const ui = runPageInitial(SEED);
    const before = skeletonScreenState(SEED, skeleton, whole, ui);
    for (const observedEpoch of [epoch - 1, epoch, epoch + 1])
      for (const value of [RunLiveness.ALIVE, RunLiveness.INTERRUPTED, RunLiveness.UNSPECIFIED]) {
        const screen = skeletonScreenState(SEED, skeleton, whole, ui, {
          epoch: observedEpoch,
          value,
        });
        const interrupted =
          state.case === 'running' && observedEpoch === epoch && value === RunLiveness.INTERRUPTED;
        expect(headerView(screen).status.label).toBe(
          interrupted ? 'Interrupted' : headerView(before).status.label,
        );
        expect(screen.record.kind).toBe(interrupted ? 'ended' : before.record.kind);
        expect(skeleton.lifecycles.at(-1)?.lifecycle?.state).toEqual(
          create(LifecycleSchema, {state}).state,
        );
      }
  });

test('a CLI window pause target is the recorded root, not the seed path', () => {
  const {records} = storeRunFixture('short', 2, 3);
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const epoch = skeleton.epochs.at(-1)?.epoch;
  if (!epoch) throw new Error('epoch required');
  epoch.projectRoot = '/canonical/project';
  const seed = {...SEED, title: {project: '', task: null}};
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  const state = skeletonScreenState(seed, skeleton, model, runPageInitial(seed));
  expect(recordedRunRoot(skeleton)).toBe(recordedProjectRoot('/canonical/project'));
  if (state.record.kind !== 'live' && state.record.kind !== 'ended') {
    throw new Error('fixture run required');
  }
  expect(state.record.run.projectRoot).toBe('/canonical/project');
});

test('a run with no recorded root does not use the path the window remembered', () => {
  const {records} = storeRunFixture('short', 2, 3);
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const epoch = skeleton.epochs.at(-1)?.epoch;
  if (!epoch) throw new Error('epoch required');
  epoch.projectRoot = '';
  const seed = {...SEED, title: {project: '~/queue', task: null}};
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  const state = skeletonScreenState(seed, skeleton, model, runPageInitial(seed));
  expect(recordedRunRoot(skeleton)).toBeUndefined();
  if (state.record.kind === 'live' || state.record.kind === 'ended') {
    expect(state.record.run.projectRoot).toBe('');
  }
});

test('a native metric headlines the writer fact even without candidate-shaped spans', () => {
  const first = storeRunFixture('short', 1).records[0];
  if (!first) throw new Error('fixture');
  const skeleton = foldSkeleton(seedSkeleton(), [
    first,
    create(RecordSchema, {
      seq: 2n,
      epoch: 1,
      body: {case: 'metric', value: {series: 'throughput', value: 321, clock: 1}},
    }),
  ]);
  expect(skeleton.metrics[0]?.points[0]?.point?.value).toBe(321);
  const head = create(RunHeadSchema, {
    epoch: 1,
    committedSeq: 2n,
    performanceStated: true,
    performance: [{series: 'throughput', inputValue: 100, bestValue: 321}],
    objective: {
      series: {name: 'throughput', unit: 'ops/s', direction: Direction.MAX},
      bestValue: 321,
      inputValue: 100,
    },
  });
  const model = projectSkeleton(skeleton, 2n, '', {head});
  expect(model.candidates.candidates).toHaveLength(0);
  expect(heroView(model).headlines[0]?.figure).toBe('3.21×');
  expect(heroView(model).headlines[0]?.detail).toContain('321 ops/s');
  for (const committedSeq of [1n, 3n]) {
    expect(
      projectSkeleton(skeleton, 2n, '', {head: {...head, committedSeq}}).headline,
    ).toBeUndefined();
  }
});

for (const reversed of [false, true]) {
  test(`native event fold preserves objective tiles and trends with a writer headline (${reversed})`, () => {
    const records = storeRunFixture('facts', 2).records.map(record => clone(RecordSchema, record));
    // Every declaration of the epoch (its opening and the amendment that states the input).
    const declarations = records.flatMap(record =>
      record.body.case === 'epochStarted' || record.body.case === 'declarationAmended'
        ? [record.body.value]
        : [],
    );
    for (const declaration of declarations) {
      if (!declaration.run) throw new Error('fixture');
      declaration.run.objectives.push(
        create(ObjectiveSchema, {series: 'latency', direction: Direction.MIN}),
      );
      if (reversed) declaration.run.objectives.reverse();
      declaration.series.push(
        create(MetricSeriesDeclSchema, {
          ...declaration.series[0],
          name: 'latency',
          unit: 'ms',
          direction: Direction.MIN,
        }),
      );
    }
    const events = records
      .flatMap(record => {
        if (record.body.case !== 'metric' || record.body.value.series !== 'throughput')
          return [record];
        return [
          record,
          create(RecordSchema, {
            ...record,
            body: {
              case: 'metric',
              value: {
                ...record.body.value,
                series: 'latency',
                value: 1000 / record.body.value.value,
              },
            },
          }),
        ];
      })
      .map((record, index) => create(RecordSchema, {...record, seq: BigInt(index + 1)}));
    const skeleton = foldSkeleton(seedSkeleton(), events);
    const seq = skeleton.throughSeq ?? 0n;
    const original = heroView(projectSkeleton(skeleton, seq)).headlines;
    const head = create(RunHeadSchema, {
      epoch: 1,
      committedSeq: seq,
      performanceStated: true,
      performance: [{series: 'throughput', inputValue: 100, bestValue: 320}],
      objective: {
        series: {name: 'throughput', unit: 'ops/s', direction: Direction.MAX},
        inputValue: 100,
        bestValue: 320,
      },
    });
    const tiles = heroView(projectSkeleton(skeleton, seq, '', {head})).headlines;
    expect(tiles.map(tile => tile.id)).toEqual(original.map(tile => tile.id));
    expect(tiles).toHaveLength(2);
    for (const tile of original) {
      const updated = tiles.find(each => each.id === tile.id);
      if (tile.id === 'throughput') {
        expect(updated?.figure).toBe('3.20×');
        expect(updated?.detail).toBe('320 ops/s · input 100 ops/s');
      } else {
        expect(updated).toEqual(tile);
      }
    }
  });
}

test('native turn costs, cache tokens and inline or attached typed replies survive keyframes', () => {
  for (const attached of [false, true]) {
    const fixture = storeRunFixture('short', 1);
    const initial = foldSkeleton(seedSkeleton(), fixture.records);
    const span = Object.values(seekSkeleton(initial, initial.throughSeq ?? 0n).spans).find(
      each => each.label?.kind === 'agent_turn',
    );
    if (!span?.label?.attrs) throw new Error('fixture');
    const reply = {
      kind: 'Result',
      json_schema: {type: 'object'},
      value: {answer: attached ? 'x'.repeat(65537) : 'yes'},
    };
    const attrs = fromBinary(AgentTurnAttrsSchema, span.label.attrs.value);
    attrs.replyJson = attached ? '' : JSON.stringify(reply);
    attrs.usage = create(TokenUsageSchema, {
      input: 3n,
      cacheRead: 7n,
      cacheCreation: 11n,
      output: 5n,
      costUsd: 1.25,
    });
    const seq = (initial.throughSeq ?? 0n) + 1n;
    const updated = clone(SpanSchema, span);
    if (!updated.label) throw new Error('fixture');
    updated.label.attrs = create(AnySchema, {
      typeUrl: `type.googleapis.com/${AgentTurnAttrsSchema.typeName}`,
      value: toBinary(AgentTurnAttrsSchema, attrs),
    });
    const skeleton = foldSkeleton(initial, [
      create(RecordSchema, {
        seq,
        epoch: 1,
        body: {case: 'span', value: {change: Change.UPDATED, span: updated}},
      }),
    ]);
    const hash = 'a'.repeat(64);
    const content = {text: {[hash]: JSON.stringify(reply)}, replies: {[span.id]: [{seq, hash}]}};
    for (const history of [
      skeleton,
      seedSkeleton(create(KeyframeSchema, {...skeleton, throughSeq: seq, logOffset: 8n})),
    ]) {
      const model = projectSkeleton(history, seq, '', {content});
      const state = skeletonScreenState(
        SEED,
        history,
        model,
        runPageInitial(SEED),
        undefined,
        content,
      );
      const usage = state.run.core?.usageRecords.find(row => row.span_id === span.id);
      expect(usage?.cost_usd).toBe(1.25);
      expect(usage?.tokens.cache_read).toBe(7);
      expect(usage?.tokens.cache_write).toBe(11);
      expect(state.run.core?.transcript.find(row => row.spanId === span.id)?.reply).toEqual(reply);
      const before = skeletonScreenState(
        SEED,
        history,
        model,
        {...runPageInitial(SEED), view: {at: Number(seq - 1n)}},
        undefined,
        content,
      );
      expect(before.run.core?.transcript.find(row => row.spanId === span.id)).toBeUndefined();
    }
  }
});

test('retained real usage and cache costs match the old projection through replay and a saved keyframe', () => {
  const source = JSON.parse(
    readFileSync(
      new URL(
        '../../../../../tests/conformance/fixtures/usage-records/codex-ten-rounds-trimmed.json',
        import.meta.url,
      ),
      'utf8',
    ),
  ) as {records: UsageRecord[]};
  const root = new URL('../../../../../proto/testdata/conversion/usage/', import.meta.url);
  const frame = fromBinary(KeyframeSchema, readFileSync(new URL('expected.pb', root)));
  const values: string[] = JSON.parse(readFileSync(new URL('records.base64.json', root), 'utf8'));
  const records = values.map(value => Uint8Array.from(Buffer.from(value, 'base64')));
  const expected = totalUsage(source.records);
  expect(expected.turns).toBe(8);
  expect(expected.tokens.cacheRead).toBeGreaterThan(0);
  expect(expected.costUsd).toBeGreaterThan(0);
  const spend = (row: UsageRecord) => ({
    tokens: row.tokens,
    cost: row.cost_usd,
    duration: row.duration_ms,
    harness: row.harness,
    model: row.model,
    client: row.client,
    effort: row.reasoning_effort,
    version: row.harness_version,
  });
  for (const checkpoint of [false, true]) {
    const load = ingestSkeleton(
      create(SkeletonResponseSchema, {
        ...(checkpoint ? {keyframe: toBinary(KeyframeSchema, frame)} : {records}),
        committedSeq: frame.throughSeq,
        nextSeq: frame.throughSeq + 1n,
        complete: true,
      }),
    );
    const reading = readSkeleton(load.skeleton, seekSkeleton(load.skeleton, frame.throughSeq));
    const actual = totalUsage(reading.core.usageRecords);
    expect(actual.tokens).toEqual(expected.tokens);
    expect(actual.turns).toBe(expected.turns);
    // A keyframe's maps may enumerate turns in another order, changing only floating-point rounding.
    expect(actual.costUsd).toBeCloseTo(expected.costUsd ?? Number.NaN, 14);
    const ordered = (rows: readonly UsageRecord[]) =>
      rows.map(spend).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
    expect(ordered(reading.core.usageRecords)).toEqual(ordered(source.records));
  }
});

test('regression: native planning-call markers survive keyframes across call counts', () => {
  const replay = fromBinary(
    ClientReplaySchema,
    readFileSync(
      new URL('../../../../libs/run-fixtures/src/native/finished-run.pb', import.meta.url),
    ),
  );
  const records = replay.batches.flatMap(batch =>
    batch.records.map(bytes => fromBinary(RecordSchema, bytes)),
  );
  const base = foldSkeleton(seedSkeleton(), records);
  const planning = Object.values(base.spans).find(
    history => history.versions[0]?.span?.label?.kind === 'planning_call',
  )?.versions[0];
  if (!planning?.span) throw new Error('Canonical scenario requires planning calls');
  for (const count of [0, 1, 2, 15, 24]) {
    const spans = Object.fromEntries(
      Object.entries(base.spans).filter(
        ([, history]) => history.versions[0]?.span?.label?.kind !== 'planning_call',
      ),
    );
    for (let index = 0; index < count; index += 1) {
      const id = `planning-${index}`;
      spans[id] = create(SpanHistorySchema, {
        versions: [{...planning, seq: BigInt(index + 2), span: {...planning.span, id}}],
      });
    }
    const skeleton = {...base, spans};
    const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
    const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
    expect([...state.run.trace.calls]).toEqual(
      Array.from({length: count}, (_, index) => index + 2),
    );
  }
});
