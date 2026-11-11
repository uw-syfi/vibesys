import {createHash} from 'node:crypto';
import {create, toBinary} from '@bufbuild/protobuf';
import {anyPack, FileDescriptorSetSchema, timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentClient,
  AgentTurnAttrsSchema,
  BlobRefSchema,
  Change,
  DescriptorSetSchema,
  Direction,
  EpochStartedSchema,
  EpochVersionSchema,
  InputMeasurementSchema,
  KeyframeSchema,
  LegacyEventSchema,
  LifecycleSchema,
  LifecycleVersionSchema,
  MetricHistorySchema,
  MetricPointSchema,
  MetricSeriesDeclSchema,
  MetricVersionSchema,
  ObjectiveSchema,
  RecordSchema,
  RunHeadSchema,
  RuntimeType,
  SeriesRole,
  SkeletonResponseSchema,
  SpanHistorySchema,
  SpanKindDeclSchema,
  SpanRecordSchema,
  SpanSchema,
  SpanStatus,
  SpanVersionSchema,
  type Record as StoreRecord,
  TailResponseSchema,
  TokenUsageSchema,
  TurnUsageDetailSchema,
} from '@vibesys/proto';
import fc from 'fast-check';
import {
  emptyRunDetail,
  ingestRunDetailSkeleton,
  ingestRunDetailTail,
  projectRunDetail,
  type RunDetailState,
} from './index.js';

const RUN = 'run-dynamic';
const HASH = '0'.repeat(64);
const AT = timestampFromMs(1_000);
const descriptorSet = create(FileDescriptorSetSchema, {
  file: [
    {
      name: 'agent.proto',
      package: 'vibesys.store.v1',
      syntax: 'proto3',
      messageType: [{name: 'AgentTurnAttrs'}],
    },
  ],
});
const descriptorBytes = toBinary(FileDescriptorSetSchema, descriptorSet);
const descriptorHash = createHash('sha256').update(descriptorBytes).digest('hex');
const unknownDescriptorSet = create(FileDescriptorSetSchema, {
  file: [
    {
      name: 'unknown.proto',
      package: 'example',
      syntax: 'proto3',
      messageType: [{name: 'OtherAttrs'}],
    },
  ],
});
const unknownDescriptorBytes = toBinary(FileDescriptorSetSchema, unknownDescriptorSet);
const unknownDescriptorHash = createHash('sha256').update(unknownDescriptorBytes).digest('hex');

function header(sequence = 1n, epoch = 1): StoreRecord {
  return declaration(sequence, 'epochStarted', Direction.MAX, 'items/s', epoch);
}

function declaration(
  sequence: bigint,
  bodyCase: 'epochStarted' | 'declarationAmended',
  direction: Direction,
  unit: string,
  epochNumber = 1,
): StoreRecord {
  const objective = direction === Direction.NONE ? [] : [{series: 'throughput', direction}];
  const epoch = create(EpochStartedSchema, {
    runId: RUN,
    epoch: epochNumber,
    treeHash: 'tree',
    apiSchemaHash: HASH,
    storeSchemaHash: HASH,
    vibesysVersion: 'test',
    run: {
      strategy: 'natural-language',
      input: 'base',
      objective: create(BlobRefSchema, {sha256: HASH, mediaType: 'text/plain'}),
      objectives: objective,
      expectedRoles: ['planner', 'bespoke-reviewer'],
    },
    spanKinds: [
      create(SpanKindDeclSchema, {
        kind: 'agent_turn',
        attrsType: AgentTurnAttrsSchema.typeName,
        timeline: true,
      }),
    ],
    series: [
      create(MetricSeriesDeclSchema, {
        name: 'throughput',
        unit,
        direction,
        role: direction === Direction.NONE ? SeriesRole.REFERENCE : SeriesRole.OBJECTIVE,
      }),
    ],
    descriptors: descriptorSet,
  });
  return create(RecordSchema, {
    seq: sequence,
    epoch: epochNumber,
    at: AT,
    body:
      bodyCase === 'epochStarted'
        ? {case: 'epochStarted', value: epoch}
        : {case: 'declarationAmended', value: epoch},
  });
}

interface AgentOptions {
  readonly sequence: bigint;
  readonly epoch?: number;
  readonly id?: string;
  readonly role?: string;
  readonly status?: SpanStatus;
  readonly usage?: boolean;
  readonly kind?: string;
}

function agent(options: AgentOptions): StoreRecord {
  const id = options.id ?? 'agent-span';
  const attrs = create(AgentTurnAttrsSchema, {
    agent: `agent:${id}`,
    role: options.role ?? 'bespoke-reviewer',
    stage: 'implementation',
    startedBy: ['orchestrator'],
    model: 'model-x',
    provider: 'provider-y',
    reasoningEffort: 'high',
    harnessVersion: '7',
    client: AgentClient.PROVIDER,
    usage: options.usage
      ? create(TokenUsageSchema, {input: 11n, output: 7n, costUsd: 0.25, durationMs: 900n})
      : undefined,
    usageDetails: options.usage
      ? [create(TurnUsageDetailSchema, {providerSessionId: 'session-open'})]
      : [],
  });
  return create(RecordSchema, {
    seq: options.sequence,
    epoch: options.epoch ?? 1,
    at: AT,
    body: {
      case: 'span',
      value: create(SpanRecordSchema, {
        change: options.status === SpanStatus.DONE ? Change.ENDED : Change.STARTED,
        span: create(SpanSchema, {
          id,
          runtimeType: RuntimeType.OPERATION,
          name: id,
          status: options.status ?? SpanStatus.RUNNING,
          startedAt: 1,
          endedAt: options.status === SpanStatus.DONE ? 2 : undefined,
          label: {
            kind: options.kind ?? 'agent_turn',
            title: id,
            attrs: anyPack(AgentTurnAttrsSchema, attrs),
          },
        }),
      }),
    },
  });
}

function metric(sequence: bigint, value: number, series = 'throughput'): StoreRecord {
  return create(RecordSchema, {
    seq: sequence,
    epoch: 1,
    at: AT,
    body: {
      case: 'metric',
      value: create(MetricPointSchema, {
        series,
        value,
        clock: Number(sequence),
      }),
    },
  });
}

function headerWithSeries(names: readonly string[]): StoreRecord {
  const base = epochOf(header());
  if (!base.run) throw new Error('test header has no run description');
  const epoch = create(EpochStartedSchema, {
    ...base,
    run: {
      ...base.run,
      objectives: names.map(series => create(ObjectiveSchema, {series, direction: Direction.MAX})),
    },
    series: names.map(name =>
      create(MetricSeriesDeclSchema, {
        name,
        unit: 'items/s',
        direction: Direction.MAX,
        role: SeriesRole.OBJECTIVE,
      }),
    ),
  });
  return create(RecordSchema, {
    seq: 1n,
    epoch: 1,
    at: AT,
    body: {case: 'epochStarted', value: epoch},
  });
}

function legacy(sequence: bigint): StoreRecord {
  return create(RecordSchema, {
    seq: sequence,
    body: {case: 'legacyEvent', value: create(LegacyEventSchema, {json: '{}'})},
  });
}

function runningLifecycle() {
  return create(LifecycleSchema, {state: {case: 'running', value: {}}});
}

function lifecycle(sequence: bigint, epoch: number): StoreRecord {
  return create(RecordSchema, {
    seq: sequence,
    epoch,
    at: AT,
    body: {case: 'lifecycle', value: runningLifecycle()},
  });
}

function publishedHead(sequence: bigint, epoch = 1) {
  return create(RunHeadSchema, {
    lifecycle: runningLifecycle(),
    strategy: 'natural-language',
    objective: {
      series: {
        name: 'throughput',
        unit: 'items/s',
        direction: Direction.MAX,
        role: SeriesRole.OBJECTIVE,
      },
      inputStated: true,
    },
    committedSeq: sequence,
    epoch,
    performanceStated: true,
  });
}

function snapshot(
  records: readonly StoreRecord[],
  committedSequence: bigint = records.at(-1)?.seq ?? 0n,
) {
  const through = records.at(-1)?.seq;
  return create(SkeletonResponseSchema, {
    records: records.map(record => toBinary(RecordSchema, record)),
    committedSeq: committedSequence,
    nextSeq: through === undefined ? 1n : through + 1n,
    complete: (through ?? 0n) === committedSequence,
    head: publishedHead(committedSequence),
  });
}

function tail(records: readonly StoreRecord[]) {
  return create(TailResponseSchema, {
    records: records.map(record => toBinary(RecordSchema, record)),
    throughSeq: records.at(-1)?.seq ?? 0n,
    head: publishedHead(records.at(-1)?.seq ?? 0n),
  });
}

function applied(update: ReturnType<typeof ingestRunDetailSkeleton>): RunDetailState {
  expect(update.kind).toBe('applied');
  return update.state;
}

function epochOf(record: StoreRecord) {
  return record.body.value as ReturnType<typeof create<typeof EpochStartedSchema>>;
}

function spanOf(record: StoreRecord) {
  const span = (record.body.value as {span?: ReturnType<typeof create<typeof SpanSchema>>}).span;
  if (!span) throw new Error('test record is not a span');
  return span;
}

describe('generic run detail', () => {
  it('projects open agent identity, lifecycle, usage, objectives, and declared metrics', () => {
    const initial = applied(
      ingestRunDetailSkeleton(
        emptyRunDetail(RUN),
        snapshot([header(), agent({sequence: 2n}), metric(3n, 12)]),
      ),
    );
    const updated = ingestRunDetailTail(
      initial,
      tail([agent({sequence: 4n, status: SpanStatus.DONE, usage: true}), metric(5n, 15)]),
    );
    expect(updated.kind).toBe('applied');
    const view = projectRunDetail(updated.state);

    expect(view.agents.values).toMatchObject([
      {
        agentId: 'agent:agent-span',
        role: 'bespoke-reviewer',
        model: 'model-x',
        provider: 'provider-y',
        client: 'provider',
        providerSessionIds: ['session-open'],
        lifecycle: 'done',
        usage: {inputTokens: 11n, outputTokens: 7n, costUsd: 0.25},
      },
    ]);
    expect(view.metrics.values[0]).toMatchObject({
      name: 'throughput',
      unit: 'items/s',
      direction: 'max',
      role: 'objective',
      points: [{value: 12}, {value: 15}],
    });
    expect(view.objectives.values).toMatchObject([{series: 'throughput', direction: 'max'}]);
    expect(view.workstreams.availability).toEqual({
      kind: 'unavailable',
      reason: 'projection-not-installed',
    });
    expect(view.correctness.availability).toEqual({
      kind: 'unavailable',
      reason: 'projection-not-installed',
    });
  });

  it('uses an amended declaration for existing metrics and objectives across every fold split', () => {
    fc.assert(
      fc.property(
        fc.double({min: -1_000, max: 1_000, noNaN: true, noDefaultInfinity: true}),
        fc.double({min: -1_000, max: 1_000, noNaN: true, noDefaultInfinity: true}),
        fc.constantFrom(Direction.MIN, Direction.MAX),
        (before, after, direction) => {
          const records = [
            declaration(1n, 'epochStarted', Direction.NONE, 'items'),
            metric(2n, before),
            declaration(3n, 'declarationAmended', direction, 'items/s'),
            metric(4n, after),
          ];
          const expectedDirection = direction === Direction.MIN ? 'min' : 'max';
          const whole = projectRunDetail(
            applied(ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records))),
          );
          let split = applied(
            ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records.slice(0, 2), 4n)),
          );
          split = ingestRunDetailTail(split, tail([records[2] as StoreRecord])).state;

          expect(projectRunDetail(split)).toMatchObject({
            metrics: {
              values: [
                {
                  unit: 'items/s',
                  direction: expectedDirection,
                  role: 'objective',
                  source: {sequence: 3n},
                  points: [{value: before, source: {sequence: 2n}}],
                },
              ],
            },
            objectives: {
              values: [
                {
                  series: 'throughput',
                  direction: expectedDirection,
                  source: {sequence: 3n},
                },
              ],
            },
          });

          const keyframed = projectRunDetail(
            applied(
              ingestRunDetailSkeleton(
                emptyRunDetail(RUN),
                create(SkeletonResponseSchema, {
                  keyframe: toBinary(
                    KeyframeSchema,
                    create(KeyframeSchema, {
                      throughSeq: 3n,
                      logOffset: 8n,
                      epochs: [
                        create(EpochVersionSchema, {
                          seq: 1n,
                          epoch: records[0]?.body.value as ReturnType<
                            typeof create<typeof EpochStartedSchema>
                          >,
                          startsEpoch: true,
                        }),
                        create(EpochVersionSchema, {
                          seq: 3n,
                          epoch: records[2]?.body.value as ReturnType<
                            typeof create<typeof EpochStartedSchema>
                          >,
                        }),
                      ],
                      metrics: [
                        create(MetricHistorySchema, {
                          series: 'throughput',
                          points: [
                            create(MetricVersionSchema, {
                              seq: 2n,
                              point: records[1]?.body.value as ReturnType<
                                typeof create<typeof MetricPointSchema>
                              >,
                            }),
                          ],
                        }),
                      ],
                    }),
                  ),
                  committedSeq: 3n,
                  nextSeq: 4n,
                  complete: true,
                  head: publishedHead(3n),
                }),
              ),
            ),
          );
          expect(keyframed.metrics.values).toEqual(projectRunDetail(split).metrics.values);
          expect(keyframed.objectives.values).toEqual(projectRunDetail(split).objectives.values);

          split = ingestRunDetailTail(split, tail([records[3] as StoreRecord])).state;
          expect(projectRunDetail(split)).toEqual(whole);
          expect(projectRunDetail(split).metrics.values[0]?.source.sequence).toBe(3n);
        },
      ),
      {numRuns: 80},
    );
  });

  it('makes replay idempotent, stops at a gap, and lets a fresh skeleton repair it', () => {
    const records = [header(), metric(2n, 2), metric(3n, 3), metric(4n, 4), metric(5n, 5)];
    const held = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records.slice(0, 3), 5n)),
    );
    const duplicate = ingestRunDetailTail(held, tail(records.slice(1, 3)));
    expect(duplicate).toEqual({kind: 'duplicate', state: held});

    const gap = ingestRunDetailTail(held, tail([records[4] as StoreRecord]));
    expect(gap.kind).toBe('refresh-required');
    expect(projectRunDetail(gap.state).freshness).toMatchObject({
      kind: 'refresh-required',
      expectedSequence: 4n,
      observedSequence: 5n,
    });
    expect(ingestRunDetailTail(gap.state, tail([records[3] as StoreRecord])).kind).toBe(
      'refresh-required',
    );

    const repaired = ingestRunDetailSkeleton(gap.state, snapshot(records));
    expect(repaired.kind).toBe('applied');
    expect(projectRunDetail(repaired.state).freshness).toEqual({
      kind: 'current',
      throughSequence: 5n,
    });
  });

  it('rejects a headed snapshot cursor that skips part of the ordered prefix', () => {
    const previous = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot([header(), metric(2n, 2)])),
    );
    const skipped = ingestRunDetailSkeleton(
      previous,
      create(SkeletonResponseSchema, {
        records: [toBinary(RecordSchema, header())],
        committedSeq: 3n,
        nextSeq: 3n,
        complete: false,
        head: publishedHead(3n),
      }),
    );
    expect(skipped.kind).toBe('rejected-snapshot');
    expect(skipped.state).toBe(previous);

    const empty = ingestRunDetailSkeleton(
      emptyRunDetail(RUN),
      create(SkeletonResponseSchema, {
        committedSeq: 0n,
        nextSeq: 0n,
        complete: true,
        head: publishedHead(0n),
      }),
    );
    expect(empty.kind).toBe('applied');
    expect(empty.state.cursor).toMatchObject({kind: 'ready', nextSequence: 1n});
  });

  it('uses head presence, not record bodies, to distinguish lower-bound legacy sequences', () => {
    const held = applied(
      ingestRunDetailSkeleton(
        emptyRunDetail(RUN),
        create(SkeletonResponseSchema, {
          records: [legacy(0n), legacy(3n)].map(record => toBinary(RecordSchema, record)),
          committedSeq: 3n,
          nextSeq: 6n,
          complete: false,
        }),
      ),
    );
    const continued = ingestRunDetailTail(
      held,
      create(TailResponseSchema, {
        records: [legacy(6n), legacy(9n)].map(record => toBinary(RecordSchema, record)),
        throughSeq: 9n,
      }),
    );
    expect(continued.kind).toBe('applied');
    expect(continued.state).toMatchObject({
      throughSequence: 9n,
      cursor: {kind: 'ready', nextSequence: 10n, sequencePolicy: 'lower-bound'},
    });
    expect(projectRunDetail(continued.state).freshness.kind).toBe('catching-up');

    const publishedGap = ingestRunDetailSkeleton(
      emptyRunDetail(RUN),
      snapshot([legacy(1n), legacy(3n)]),
    );
    expect(publishedGap.kind).toBe('rejected-snapshot');

    const published = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot([legacy(1n)], 3n)),
    );
    expect(ingestRunDetailTail(published, tail([legacy(3n)])).kind).toBe('refresh-required');
    expect(
      ingestRunDetailTail(
        published,
        create(TailResponseSchema, {
          records: [toBinary(RecordSchema, legacy(2n))],
          throughSeq: 2n,
        }),
      ).kind,
    ).toBe('refresh-required');
  });

  it('keeps unknown descriptors explicit and rejects a falsely addressed descriptor set', () => {
    const epoch = header().body.value as ReturnType<typeof create<typeof EpochStartedSchema>>;
    const withoutInline = create(EpochStartedSchema, {...epoch, descriptors: undefined});
    const span = (
      agent({sequence: 2n}).body.value as {span?: ReturnType<typeof create<typeof SpanSchema>>}
    ).span;
    const keyframe = create(KeyframeSchema, {
      throughSeq: 2n,
      logOffset: 8n,
      epochs: [
        create(EpochVersionSchema, {
          seq: 1n,
          epoch: withoutInline,
          descriptorsSha256: descriptorHash,
          startsEpoch: true,
        }),
      ],
      spans: {
        'agent-span': create(SpanHistorySchema, {
          versions: [create(SpanVersionSchema, {seq: 2n, span})],
        }),
      },
    });
    const response = create(SkeletonResponseSchema, {
      keyframe: toBinary(KeyframeSchema, keyframe),
      committedSeq: 2n,
      nextSeq: 3n,
      complete: true,
      head: publishedHead(2n),
    });
    const missing = ingestRunDetailSkeleton(emptyRunDetail(RUN), response);
    expect(missing.kind).toBe('rejected-snapshot');
    if (missing.kind === 'rejected-snapshot') {
      expect(missing.reason).toContain('keyframe descriptor set: missing');
    }

    const unknownKeyframe = create(KeyframeSchema, {
      ...keyframe,
      epochs: [
        create(EpochVersionSchema, {
          seq: 1n,
          epoch: withoutInline,
          descriptorsSha256: unknownDescriptorHash,
          startsEpoch: true,
        }),
      ],
    });
    const unknown = ingestRunDetailSkeleton(
      emptyRunDetail(RUN),
      create(SkeletonResponseSchema, {
        ...response,
        keyframe: toBinary(KeyframeSchema, unknownKeyframe),
        descriptorSets: [
          create(DescriptorSetSchema, {
            sha256: unknownDescriptorHash,
            data: unknownDescriptorBytes,
          }),
        ],
      }),
    );
    expect(unknown.kind).toBe('applied');
    expect(projectRunDetail(unknown.state).agents).toMatchObject({
      availability: {kind: 'partial', reasons: ['descriptor-unknown']},
      values: [],
    });

    const resolved = ingestRunDetailSkeleton(
      emptyRunDetail(RUN),
      create(SkeletonResponseSchema, {
        ...response,
        descriptorSets: [
          create(DescriptorSetSchema, {sha256: descriptorHash, data: descriptorBytes}),
        ],
      }),
    );
    expect(resolved.kind).toBe('applied');
    expect(projectRunDetail(resolved.state).agents.values).toMatchObject([
      {agentId: 'agent:agent-span', role: 'bespoke-reviewer'},
    ]);

    const falseAddress = create(SkeletonResponseSchema, {
      ...response,
      descriptorSets: [create(DescriptorSetSchema, {sha256: HASH, data: descriptorBytes})],
    });
    const rejected = ingestRunDetailSkeleton(emptyRunDetail(RUN), falseAddress);
    expect(rejected.kind).toBe('rejected-snapshot');
    if (rejected.kind === 'rejected-snapshot') {
      expect(rejected.reason).toContain('descriptor set: expected');
    }
  });

  it('reconstructs bounded keyframes from complete ephemeral epoch and descriptor evidence', () => {
    const epochOne = declaration(1n, 'epochStarted', Direction.NONE, 'items', 1);
    const point = metric(2n, 7);
    const amendment = declaration(3n, 'declarationAmended', Direction.MAX, 'items/s', 1);
    const running = lifecycle(4n, 1);
    const epochTwo = header(5n, 2);
    const limits = {maxEpochVersions: 1, maxDescriptorSets: 1};
    const raw = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN, limits),
          snapshot([epochOne, point, amendment, running, epochTwo]),
        ),
      ),
    );
    const compacted = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN, limits),
          create(SkeletonResponseSchema, {
            keyframe: toBinary(
              KeyframeSchema,
              create(KeyframeSchema, {
                throughSeq: 5n,
                logOffset: 8n,
                epochs: [
                  create(EpochVersionSchema, {
                    seq: 1n,
                    epoch: epochOf(epochOne),
                    startsEpoch: true,
                  }),
                  create(EpochVersionSchema, {
                    seq: 3n,
                    epoch: epochOf(amendment),
                  }),
                  create(EpochVersionSchema, {
                    seq: 5n,
                    epoch: epochOf(epochTwo),
                    startsEpoch: true,
                  }),
                ],
                lifecycles: [
                  create(LifecycleVersionSchema, {
                    seq: 4n,
                    lifecycle: runningLifecycle(),
                  }),
                ],
                metrics: [
                  create(MetricHistorySchema, {
                    series: 'throughput',
                    points: [
                      create(MetricVersionSchema, {
                        seq: 2n,
                        point: point.body.value as ReturnType<
                          typeof create<typeof MetricPointSchema>
                        >,
                      }),
                    ],
                  }),
                ],
              }),
            ),
            committedSeq: 5n,
            nextSeq: 6n,
            complete: true,
            head: publishedHead(5n, 2),
          }),
        ),
      ),
    );
    expect(compacted).toEqual(raw);
    expect(compacted.lifecycle?.source).toEqual({kind: 'run-record', sequence: 4n, epoch: 1});
    expect(compacted.metrics.values[0]).toMatchObject({
      unit: 'items/s',
      source: {sequence: 3n, epoch: 1},
      points: [{source: {sequence: 2n, epoch: 1}}],
    });
    expect(compacted.truncation.epochEvictions).toBe(2);
    expect(compacted.agents.availability).toEqual({
      kind: 'partial',
      reasons: ['state-truncated'],
    });
    expect(compacted.metrics.availability).toEqual({
      kind: 'partial',
      reasons: ['state-truncated'],
    });
    expect(compacted.objectives.availability).toEqual({
      kind: 'partial',
      reasons: ['state-truncated'],
    });

    const addressedEpochOne = create(EpochStartedSchema, {
      ...epochOf(epochOne),
      descriptors: undefined,
    });
    const descriptorEpochTwo = header(3n, 2);
    const addressedEpochTwo = create(EpochStartedSchema, {
      ...epochOf(descriptorEpochTwo),
      descriptors: undefined,
    });
    const bothDescriptors = ingestRunDetailSkeleton(
      emptyRunDetail(RUN, {maxDescriptorSets: 1}),
      create(SkeletonResponseSchema, {
        keyframe: toBinary(
          KeyframeSchema,
          create(KeyframeSchema, {
            throughSeq: 3n,
            logOffset: 8n,
            epochs: [
              create(EpochVersionSchema, {
                seq: 1n,
                epoch: addressedEpochOne,
                descriptorsSha256: descriptorHash,
                startsEpoch: true,
              }),
              create(EpochVersionSchema, {
                seq: 3n,
                epoch: addressedEpochTwo,
                descriptorsSha256: unknownDescriptorHash,
                startsEpoch: true,
              }),
            ],
          }),
        ),
        descriptorSets: [
          create(DescriptorSetSchema, {sha256: descriptorHash, data: descriptorBytes}),
          create(DescriptorSetSchema, {
            sha256: unknownDescriptorHash,
            data: unknownDescriptorBytes,
          }),
        ],
        committedSeq: 3n,
        nextSeq: 4n,
        complete: true,
        head: publishedHead(3n, 2),
      }),
    );
    expect(bothDescriptors.kind).toBe('applied');
    expect(bothDescriptors.state.descriptors).toHaveLength(1);
    expect(bothDescriptors.state.truncation.descriptorSetEvictions).toBe(1);
    expect(projectRunDetail(bothDescriptors.state).agents.availability).toEqual({
      kind: 'available',
    });
  });

  it('canonicalizes retained-writer input amendments that do not change declarations', () => {
    const first = header();
    const inputMeasurement = create(InputMeasurementSchema, {
      state: {case: 'measured', value: {series: 'throughput', value: 7}},
    });
    const point = create(RecordSchema, {...metric(2n, 7), inputMeasurement});
    const firstEpoch = epochOf(first);
    if (!firstEpoch.run) throw new Error('test header has no run description');
    const inferredEpoch = create(EpochStartedSchema, {
      ...firstEpoch,
      run: {...firstEpoch.run, inputMeasurement},
    });
    const limits = {maxEpochVersions: 1};
    const raw = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot([first, point]))),
    );
    const compacted = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN, limits),
          create(SkeletonResponseSchema, {
            keyframe: toBinary(
              KeyframeSchema,
              create(KeyframeSchema, {
                throughSeq: 2n,
                logOffset: 8n,
                epochs: [
                  create(EpochVersionSchema, {
                    seq: 1n,
                    epoch: firstEpoch,
                    startsEpoch: true,
                  }),
                  create(EpochVersionSchema, {seq: 2n, epoch: inferredEpoch}),
                ],
                metrics: [
                  create(MetricHistorySchema, {
                    series: 'throughput',
                    points: [
                      create(MetricVersionSchema, {
                        seq: 2n,
                        point: point.body.value as ReturnType<
                          typeof create<typeof MetricPointSchema>
                        >,
                      }),
                    ],
                  }),
                ],
              }),
            ),
            committedSeq: 2n,
            nextSeq: 3n,
            complete: true,
            head: publishedHead(2n),
          }),
        ),
      ),
    );
    expect(compacted).toEqual(raw);
    expect(compacted.truncation.epochEvictions).toBe(0);
    expect(compacted.metrics.values[0]?.source.sequence).toBe(1n);
    expect(compacted.objectives.values[0]?.source.sequence).toBe(1n);
  });

  it('keeps span declaration proof separate from cross-epoch update provenance', () => {
    const started = agent({sequence: 2n, id: 'cross-epoch'});
    const ended = agent({
      sequence: 4n,
      epoch: 2,
      id: 'cross-epoch',
      status: SpanStatus.DONE,
    });
    const records = [header(), started, header(3n, 2), ended];
    const raw = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records))),
    );
    const compacted = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN),
          create(SkeletonResponseSchema, {
            keyframe: toBinary(
              KeyframeSchema,
              create(KeyframeSchema, {
                throughSeq: 4n,
                logOffset: 8n,
                epochs: [
                  create(EpochVersionSchema, {
                    seq: 1n,
                    epoch: epochOf(records[0] as StoreRecord),
                    startsEpoch: true,
                  }),
                  create(EpochVersionSchema, {
                    seq: 3n,
                    epoch: epochOf(records[2] as StoreRecord),
                    startsEpoch: true,
                  }),
                ],
                spans: {
                  'cross-epoch': create(SpanHistorySchema, {
                    versions: [
                      create(SpanVersionSchema, {seq: 2n, span: spanOf(started)}),
                      create(SpanVersionSchema, {seq: 4n, span: spanOf(ended)}),
                    ],
                  }),
                },
              }),
            ),
            committedSeq: 4n,
            nextSeq: 5n,
            complete: true,
            head: publishedHead(4n, 2),
          }),
        ),
      ),
    );
    expect(compacted).toEqual(raw);
    expect(compacted.agents.values).toMatchObject([
      {agentId: 'agent:cross-epoch', lifecycle: 'done', source: {sequence: 4n, epoch: 2}},
    ]);
  });

  it('finalizes raw and keyframe evidence before applying agent bounds', () => {
    const startedA = agent({sequence: 2n, id: 'agent-a'});
    const startedB = agent({sequence: 3n, id: 'agent-b'});
    const endedA = agent({sequence: 4n, id: 'agent-a', status: SpanStatus.DONE});
    const limits = {maxAgents: 1};
    const raw = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN, limits),
          snapshot([header(), startedA, startedB, endedA]),
        ),
      ),
    );
    const compacted = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN, limits),
          create(SkeletonResponseSchema, {
            keyframe: toBinary(
              KeyframeSchema,
              create(KeyframeSchema, {
                throughSeq: 4n,
                logOffset: 8n,
                epochs: [
                  create(EpochVersionSchema, {
                    seq: 1n,
                    epoch: epochOf(header()),
                    startsEpoch: true,
                  }),
                ],
                spans: {
                  'agent-b': create(SpanHistorySchema, {
                    versions: [create(SpanVersionSchema, {seq: 3n, span: spanOf(startedB)})],
                  }),
                  'agent-a': create(SpanHistorySchema, {
                    versions: [
                      create(SpanVersionSchema, {seq: 2n, span: spanOf(startedA)}),
                      create(SpanVersionSchema, {seq: 4n, span: spanOf(endedA)}),
                    ],
                  }),
                },
              }),
            ),
            committedSeq: 4n,
            nextSeq: 5n,
            complete: true,
            head: publishedHead(4n),
          }),
        ),
      ),
    );
    expect(compacted).toEqual(raw);
    expect(compacted.agents.values).toMatchObject([
      {agentId: 'agent:agent-a', lifecycle: 'done', source: {sequence: 4n}},
    ]);
    expect(compacted.truncation.agentEvictions).toBe(1);
  });

  it('uses retained start proof after the declaration version is evicted', () => {
    const records = [
      header(),
      agent({sequence: 2n, id: 'retained'}),
      declaration(3n, 'declarationAmended', Direction.MIN, 'items/s'),
      agent({sequence: 4n, id: 'retained', status: SpanStatus.DONE}),
    ];
    const limits = {maxEpochVersions: 1};
    const whole = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records))),
    );
    let split = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records.slice(0, 2), 4n)),
    );
    split = ingestRunDetailTail(split, tail([records[2] as StoreRecord])).state;
    split = ingestRunDetailTail(split, tail([records[3] as StoreRecord])).state;

    expect(projectRunDetail(split)).toEqual(whole);
    expect(whole.agents.values).toMatchObject([
      {agentId: 'agent:retained', lifecycle: 'done', source: {sequence: 4n, epoch: 1}},
    ]);
    expect(whole.truncation.epochEvictions).toBe(1);
  });

  it('uses retained metric declaration proof after its epoch version is evicted', () => {
    const records = [header(), metric(2n, 2), header(3n, 2), metric(4n, 4)];
    const limits = {maxEpochVersions: 1};
    const whole = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records))),
    );
    const held = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records.slice(0, 3), 4n)),
    );
    const updated = ingestRunDetailTail(held, tail([records[3] as StoreRecord]));
    expect(updated.kind).toBe('applied');
    expect(projectRunDetail(updated.state)).toEqual(whole);
    expect(whole.metrics.values).toMatchObject([
      {
        name: 'throughput',
        source: {sequence: 1n, epoch: 1},
        points: [
          {value: 2, source: {sequence: 2n, epoch: 1}},
          {value: 4, source: {sequence: 4n, epoch: 1}},
        ],
      },
    ]);
  });

  it('does not use retained metric metadata against an explicit declaration removal', () => {
    const base = epochOf(header());
    if (!base.run) throw new Error('test header has no run description');
    const removed = create(RecordSchema, {
      seq: 3n,
      epoch: 1,
      at: AT,
      body: {
        case: 'declarationAmended',
        value: create(EpochStartedSchema, {
          ...base,
          run: {...base.run, objectives: []},
          series: [],
        }),
      },
    });
    const records = [header(), metric(2n, 2), removed, metric(4n, 4)];
    const limits = {maxEpochVersions: 1};
    const whole = applied(ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records)));
    const held = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records.slice(0, 3), 4n)),
    );
    const updated = ingestRunDetailTail(held, tail([records[3] as StoreRecord]));
    expect(updated.kind).toBe('applied');
    expect(projectRunDetail(updated.state)).toEqual(projectRunDetail(whole));
    expect(updated.state.undeclaredMetricPoints).toBe(1);
    expect(projectRunDetail(updated.state).metrics.values).toMatchObject([
      {name: 'throughput', points: [{value: 2}]},
    ]);
  });

  it('keeps bounded issue recovery independent of keyframe map order', () => {
    const older = agent({sequence: 2n, id: 'older-invalid', kind: 'unknown'});
    const newer = agent({sequence: 3n, id: 'newer-invalid', kind: 'unknown'});
    const limits = {maxIssues: 1};
    const raw = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot([header(), older, newer])),
      ),
    );
    const compacted = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN, limits),
          create(SkeletonResponseSchema, {
            keyframe: toBinary(
              KeyframeSchema,
              create(KeyframeSchema, {
                throughSeq: 3n,
                logOffset: 8n,
                epochs: [
                  create(EpochVersionSchema, {
                    seq: 1n,
                    epoch: epochOf(header()),
                    startsEpoch: true,
                  }),
                ],
                spans: {
                  'newer-invalid': create(SpanHistorySchema, {
                    versions: [create(SpanVersionSchema, {seq: 3n, span: spanOf(newer)})],
                  }),
                  'older-invalid': create(SpanHistorySchema, {
                    versions: [create(SpanVersionSchema, {seq: 2n, span: spanOf(older)})],
                  }),
                },
              }),
            ),
            committedSeq: 3n,
            nextSeq: 4n,
            complete: true,
            head: publishedHead(3n),
          }),
        ),
      ),
    );
    expect(compacted).toEqual(raw);
    expect(compacted.truncation.issueEvictions).toBe(1);
  });

  it('any snapshot/tail split with duplicate replay converges to the whole fold', () => {
    fc.assert(
      fc.property(
        fc.array(fc.double({min: -1_000, max: 1_000, noNaN: true, noDefaultInfinity: true}), {
          minLength: 1,
          maxLength: 30,
        }),
        fc.nat(),
        (values, rawCut) => {
          const records = [
            header(),
            ...values.map((value, index) => metric(BigInt(index + 2), value)),
          ];
          const cut = 1 + (rawCut % records.length);
          const whole = applied(ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records)));
          let split = applied(
            ingestRunDetailSkeleton(
              emptyRunDetail(RUN),
              snapshot(records.slice(0, cut), BigInt(records.length)),
            ),
          );
          for (let index = cut; index < records.length; index += 1) {
            const prior = records[index - 1];
            const next = records[index];
            if (prior) split = ingestRunDetailTail(split, tail([prior])).state;
            if (next) split = ingestRunDetailTail(split, tail([next])).state;
          }
          expect(projectRunDetail(split)).toEqual(projectRunDetail(whole));
        },
      ),
      {numRuns: 80},
    );
  });

  it('all configured bounds retain the newest facts and report truncation', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 8}), fc.integer({min: 1, max: 20}), (bound, extra) => {
        const count = bound + extra;
        const records: StoreRecord[] = [header()];
        for (let index = 0; index < count; index += 1) {
          records.push(metric(BigInt(index + 2), index));
        }
        let state = applied(
          ingestRunDetailSkeleton(
            emptyRunDetail(RUN, {maxMetricPointsPerSeries: bound}),
            snapshot(records),
          ),
        );
        expect(projectRunDetail(state).metrics).toMatchObject({
          availability: {kind: 'partial', reasons: ['state-truncated']},
          values: [{omittedPoints: extra}],
        });

        const agents = [header()];
        for (let index = 0; index < count; index += 1) {
          agents.push(agent({sequence: BigInt(index + 2), id: `agent-${index}`}));
        }
        state = applied(
          ingestRunDetailSkeleton(emptyRunDetail(RUN, {maxAgents: bound}), snapshot(agents)),
        );
        const view = projectRunDetail(state);
        expect(view.agents.values).toHaveLength(bound);
        expect(view.agents.availability).toEqual({kind: 'partial', reasons: ['state-truncated']});
        expect(view.truncation.agentEvictions).toBe(extra);
        expect(view.agents.values.at(-1)?.agentId).toBe(`agent:agent-${count - 1}`);

        const names = Array.from({length: count}, (_, index) => `series-${index}`);
        const seriesRecords = [
          headerWithSeries(names),
          ...names.map((name, index) => metric(BigInt(index + 2), index, name)),
        ];
        state = applied(
          ingestRunDetailSkeleton(
            emptyRunDetail(RUN, {maxMetricSeries: bound}),
            snapshot(seriesRecords),
          ),
        );
        const seriesView = projectRunDetail(state);
        expect(seriesView.metrics.values).toHaveLength(bound);
        expect(seriesView.metrics.values.at(-1)?.name).toBe(`series-${count - 1}`);
        expect(seriesView.metrics.availability).toEqual({
          kind: 'partial',
          reasons: ['state-truncated'],
        });
        expect(seriesView.truncation.metricSeriesEvictions).toBe(extra);
      }),
      {numRuns: 60},
    );
  });

  it('requires a fresh Skeleton when an evicted span later updates', () => {
    const held = applied(
      ingestRunDetailSkeleton(
        emptyRunDetail(RUN, {maxAgents: 1}),
        snapshot([
          header(),
          agent({sequence: 2n, id: 'older'}),
          agent({sequence: 3n, id: 'newer'}),
        ]),
      ),
    );
    const updated = ingestRunDetailTail(
      held,
      tail([agent({sequence: 4n, id: 'older', status: SpanStatus.DONE})]),
    );
    expect(updated.kind).toBe('refresh-required');
    if (updated.kind === 'refresh-required') expect(updated.reason).toBe('history-evicted');

    const repaired = applied(
      ingestRunDetailSkeleton(
        updated.state,
        snapshot([
          header(),
          agent({sequence: 2n, id: 'older'}),
          agent({sequence: 3n, id: 'newer'}),
          agent({sequence: 4n, id: 'older', status: SpanStatus.DONE}),
        ]),
      ),
    );
    expect(projectRunDetail(repaired).agents.values).toMatchObject([
      {agentId: 'agent:older', lifecycle: 'done'},
    ]);
  });

  it('requires a fresh Skeleton when an evicted metric series receives another point', () => {
    const records = [
      headerWithSeries(['series-a', 'series-b']),
      metric(2n, 2, 'series-a'),
      metric(3n, 3, 'series-b'),
      metric(4n, 4, 'series-a'),
    ];
    const limits = {maxMetricSeries: 1};
    const whole = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records))),
    );
    const held = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records.slice(0, 3), 4n)),
    );
    const updated = ingestRunDetailTail(held, tail([records[3] as StoreRecord]));
    expect(updated.kind).toBe('refresh-required');
    if (updated.kind === 'refresh-required') expect(updated.reason).toBe('history-evicted');

    const repaired = projectRunDetail(
      applied(ingestRunDetailSkeleton(updated.state, snapshot(records))),
    );
    expect(repaired).toEqual(whole);
    expect(repaired.metrics.values).toMatchObject([
      {name: 'series-a', points: [{value: 2}, {value: 4}]},
    ]);
  });

  it('requires a fresh Skeleton when an evicted invalid span updates', () => {
    const records = [
      header(),
      agent({sequence: 2n, id: 'invalid-a', kind: 'unknown'}),
      agent({sequence: 3n, id: 'invalid-b', kind: 'unknown'}),
      agent({sequence: 4n, id: 'invalid-a', kind: 'unknown', status: SpanStatus.DONE}),
    ];
    const limits = {maxIssues: 1};
    const whole = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records))),
    );
    const held = applied(
      ingestRunDetailSkeleton(emptyRunDetail(RUN, limits), snapshot(records.slice(0, 3), 4n)),
    );
    const updated = ingestRunDetailTail(held, tail([records[3] as StoreRecord]));
    expect(updated.kind).toBe('refresh-required');
    if (updated.kind === 'refresh-required') expect(updated.reason).toBe('history-evicted');

    const repaired = projectRunDetail(
      applied(ingestRunDetailSkeleton(updated.state, snapshot(records))),
    );
    expect(repaired).toEqual(whole);
    expect(repaired.truncation.issueEvictions).toBe(1);
  });
});
