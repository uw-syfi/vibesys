/** Existing Skeleton/Tail wire contracts as deterministic Fake transport recordings. */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  BlobRefSchema,
  Direction,
  EpochStartedSchema,
  HostService,
  LifecycleSchema,
  MetricPointSchema,
  MetricSeriesDeclSchema,
  RecordSchema,
  RunHeadSchema,
  SeriesRole,
  SkeletonResponseSchema,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import type {Recording, Step} from '../transport/index.js';

export const SKELETON = `/${HostService.typeName}/${HostService.method.skeleton.name}`;
export const TAIL = `/${HostService.typeName}/${HostService.method.tail.name}`;

const HASH = '0'.repeat(64);
const AT = timestampFromMs(1_000);

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
