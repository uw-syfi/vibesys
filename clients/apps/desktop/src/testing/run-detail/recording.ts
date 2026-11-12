/** Existing Skeleton/Tail wire contracts as deterministic Fake transport recordings. */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  type Any,
  AnySchema,
  anyPack,
  FileDescriptorSetSchema,
  timestampFromMs,
} from '@bufbuild/protobuf/wkt';
import {
  AgentClient,
  AgentTurnAttrsSchema,
  BlobRefSchema,
  Change,
  Direction,
  EpochStartedSchema,
  EvidenceKind,
  FrameworkSource,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  HostService,
  LifecycleSchema,
  MetricPointSchema,
  MetricSeriesDeclSchema,
  RecordSchema,
  RunHeadSchema,
  RuntimeType,
  SeriesRole,
  SkeletonResponseSchema,
  SpanKindDeclSchema,
  SpanRecordSchema,
  SpanSchema,
  SpanStatus,
  type Record as StoreRecord,
  TailResponseSchema,
  TokenUsageSchema,
  TurnUsageDetailSchema,
  WorkstreamAttrsSchema,
} from '@vibesys/proto';
import type {Recording, Step} from '../transport/index.js';

export const SKELETON = `/${HostService.typeName}/${HostService.method.skeleton.name}`;
export const TAIL = `/${HostService.typeName}/${HostService.method.tail.name}`;

const HASH = '0'.repeat(64);
const AT = timestampFromMs(1_000);
const REPRESENTATIVE_POINT_COUNT = 513;

const representativeDescriptors = create(FileDescriptorSetSchema, {
  file: [
    {
      name: 'agent.proto',
      package: 'vibesys.store.v1',
      syntax: 'proto3',
      messageType: [{name: 'AgentTurnAttrs'}],
    },
    {
      name: 'search.proto',
      package: 'vibesys.search.v1',
      syntax: 'proto3',
      messageType: [{name: 'WorkstreamAttrs'}, {name: 'GateAttrs'}],
    },
  ],
});

const publishedHead = (sequence: bigint) =>
  create(RunHeadSchema, {
    lifecycle: create(LifecycleSchema, {state: {case: 'running', value: {}}}),
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
    epoch: 1,
    performanceStated: true,
  });

/** A declaration that makes following metric records authoritative for `runId`. */
export const runHeader = (runId: string, sequence = 1n): StoreRecord =>
  create(RecordSchema, {
    seq: sequence,
    epoch: 1,
    at: AT,
    body: {
      case: 'epochStarted',
      value: create(EpochStartedSchema, {
        runId,
        epoch: 1,
        treeHash: 'tree',
        apiSchemaHash: HASH,
        storeSchemaHash: HASH,
        vibesysVersion: 'test',
        run: {
          strategy: 'natural-language',
          input: 'base',
          objective: create(BlobRefSchema, {sha256: HASH, mediaType: 'text/plain'}),
          objectives: [{series: 'throughput', direction: Direction.MAX}],
        },
        series: [
          create(MetricSeriesDeclSchema, {
            name: 'throughput',
            unit: 'items/s',
            direction: Direction.MAX,
            role: SeriesRole.OBJECTIVE,
          }),
        ],
      }),
    },
  });

export const runMetric = (sequence: bigint, value: number): StoreRecord =>
  create(RecordSchema, {
    seq: sequence,
    epoch: 1,
    at: AT,
    body: {
      case: 'metric',
      value: create(MetricPointSchema, {
        series: 'throughput',
        value,
        clock: Number(sequence),
      }),
    },
  });

const recordedSpan = (
  sequence: bigint,
  id: string,
  kind: string,
  attrs: Any,
  parent?: string,
): StoreRecord =>
  create(RecordSchema, {
    seq: sequence,
    epoch: 1,
    at: AT,
    body: {
      case: 'span',
      value: create(SpanRecordSchema, {
        change: Change.STARTED,
        span: create(SpanSchema, {
          id,
          parent,
          runtimeType: RuntimeType.OPERATION,
          name: kind,
          status: SpanStatus.RUNNING,
          startedAt: Number(sequence),
          label: {kind, title: id, attrs},
        }),
      }),
    },
  });

const representativeHeader = (runId: string): StoreRecord =>
  create(RecordSchema, {
    seq: 1n,
    epoch: 1,
    at: AT,
    body: {
      case: 'epochStarted',
      value: create(EpochStartedSchema, {
        runId,
        epoch: 1,
        treeHash: 'tree',
        apiSchemaHash: HASH,
        storeSchemaHash: HASH,
        vibesysVersion: 'test',
        run: {
          strategy: 'natural-language',
          input: 'base',
          objective: create(BlobRefSchema, {sha256: HASH, mediaType: 'text/plain'}),
          objectives: [{series: 'throughput', direction: Direction.MAX}],
        },
        spanKinds: [
          create(SpanKindDeclSchema, {
            kind: 'agent_turn',
            attrsType: AgentTurnAttrsSchema.typeName,
            timeline: true,
          }),
          create(SpanKindDeclSchema, {
            kind: 'workstream',
            attrsType: WorkstreamAttrsSchema.typeName,
            timeline: true,
          }),
          create(SpanKindDeclSchema, {
            kind: 'gate',
            attrsType: GateAttrsSchema.typeName,
            timeline: true,
          }),
          create(SpanKindDeclSchema, {
            kind: 'opaque',
            attrsType: 'example.unknown.v1.UnknownAttrs',
            timeline: true,
          }),
        ],
        series: [
          create(MetricSeriesDeclSchema, {
            name: 'throughput',
            unit: 'items/s',
            direction: Direction.MAX,
            role: SeriesRole.OBJECTIVE,
          }),
          create(MetricSeriesDeclSchema, {
            name: 'gpu_utilization',
            unit: 'percent',
            direction: Direction.NONE,
            role: SeriesRole.RESOURCE,
          }),
        ],
        descriptors: representativeDescriptors,
      }),
    },
  });

const representativeContinuation = () => {
  const metricRecords = Array.from({length: REPRESENTATIVE_POINT_COUNT}, (_, index) =>
    runMetric(BigInt(index + 6), index),
  );
  const committedSequence = BigInt(REPRESENTATIVE_POINT_COUNT + 6);
  const gpuMetric = create(RecordSchema, {
    seq: committedSequence,
    epoch: 1,
    at: AT,
    body: {
      case: 'metric',
      value: create(MetricPointSchema, {
        series: 'gpu_utilization',
        value: 75,
        clock: Number(committedSequence),
      }),
    },
  });
  return {records: [...metricRecords, gpuMetric], committedSequence} as const;
};

/** A recorded published run that crosses every renderer-facing generic detail family. */
export const representativeRun = (runId: string) => {
  const dynamicRole = 'bespoke-serving-systems-critic';
  const prefix = [
    representativeHeader(runId),
    recordedSpan(
      2n,
      'agent-dynamic',
      'agent_turn',
      anyPack(
        AgentTurnAttrsSchema,
        create(AgentTurnAttrsSchema, {
          agent: 'agent:dynamic',
          role: dynamicRole,
          stage: 'implementation',
          startedBy: ['orchestrator'],
          model: 'model-x',
          provider: 'provider-y',
          reasoningEffort: 'high',
          harnessVersion: '7',
          client: AgentClient.PROVIDER,
          usage: create(TokenUsageSchema, {input: 11n, output: 7n, costUsd: 0.25}),
          usageDetails: [create(TurnUsageDetailSchema, {providerSessionId: 'session-open'})],
        }),
      ),
      'work-root',
    ),
    recordedSpan(
      3n,
      'work-root',
      'workstream',
      anyPack(
        WorkstreamAttrsSchema,
        create(WorkstreamAttrsSchema, {
          hypothesisId: 'hypothesis-a',
          parentRevision: 'revision-base',
          idea: 'Try the typed serving path',
          sequence: 1,
        }),
      ),
    ),
    recordedSpan(
      4n,
      'gate-root',
      'gate',
      anyPack(
        GateAttrsSchema,
        create(GateAttrsSchema, {
          stage: GateStage.MEASUREMENT,
          subject: GateSubject.CANDIDATE,
          recipe: 'trusted',
          command: 'evaluate',
          failed: {
            check: EvidenceKind.CORRECTNESS,
            completed: 93n,
            required: 100n,
            unit: 'trials',
          },
          source: FrameworkSource.GPU,
          sourceLabel: 'candidate-evaluation',
        }),
      ),
      'work-root',
    ),
    recordedSpan(
      5n,
      'opaque-root',
      'opaque',
      create(AnySchema, {
        typeUrl: 'type.googleapis.com/example.unknown.v1.UnknownAttrs',
        value: Uint8Array.of(1),
      }),
    ),
  ];
  const continuation = representativeContinuation();
  return {
    dynamicRole,
    prefix,
    continuation: continuation.records,
    committedSequence: continuation.committedSequence,
  } as const;
};

export const skeleton = (
  records: readonly StoreRecord[],
  committedSeq: bigint = records.at(-1)?.seq ?? 0n,
): Step => {
  const through = records.at(-1)?.seq;
  const value: MessageInitShape<typeof SkeletonResponseSchema> = {
    records: records.map(record => toBinary(RecordSchema, record)),
    committedSeq,
    nextSeq: through === undefined ? 1n : through + 1n,
    complete: (through ?? 0n) === committedSeq,
    head: publishedHead(committedSeq),
  };
  return {frame: toBinary(SkeletonResponseSchema, create(SkeletonResponseSchema, value))};
};

export const rawSkeleton = (value: MessageInitShape<typeof SkeletonResponseSchema>): Step => ({
  frame: toBinary(SkeletonResponseSchema, create(SkeletonResponseSchema, value)),
});

export const tail = (records: readonly StoreRecord[]): Step => ({
  frame: toBinary(
    TailResponseSchema,
    create(TailResponseSchema, {
      records: records.map(record => toBinary(RecordSchema, record)),
      throughSeq: records.at(-1)?.seq ?? 0n,
      head: publishedHead(records.at(-1)?.seq ?? 0n),
    }),
  ),
});

/** Answer generic detail calls and leave every other call to `base`. */
export const servingRunDetail =
  (skeletons: (nth: number) => ReadonlyArray<Step>, tails: (nth: number) => ReadonlyArray<Step>) =>
  (base: Recording): Recording => ({
    respond: (target, nth) => {
      if (target.method === SKELETON) return skeletons(nth);
      if (target.method === TAIL) return tails(nth);
      return base.respond(target, nth);
    },
  });
