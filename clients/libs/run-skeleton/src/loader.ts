import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HostService,
  RecordSchema,
  Severity,
  SkeletonRequestSchema,
  type SkeletonResponse,
  SkeletonResponseSchema,
  type Record as StoreRecord,
  TailRequestSchema,
  type TailResponse,
  TailResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import {DescriptorCache} from './descriptors.js';
import {
  type FailuresInput,
  type SkeletonLoaderInput,
  skeletonLoaderMachine,
  type TailInput,
} from './loader-machine.js';
import {RECORD_WINDOW, type SkeletonLoad} from './loading.js';
import {checkpointRecords} from './record-facts.js';

/** Generic unary/server-stream byte bridge. Both operations must release resources on abort. */
export interface RunTransport {
  unary(method: string, request: Uint8Array, signal: AbortSignalLike): Promise<Uint8Array>;
  stream(method: string, request: Uint8Array, signal: AbortSignalLike): AsyncIterable<Uint8Array>;
}
const SERVICE = `/${HostService.typeName}/`;

/** Every record the loader reads reaches this once, so no other consumer reads the log again. */
export type RecordsListener = (records: readonly Uint8Array[]) => void;

export function skeletonLoaderMachineFor(
  transport: RunTransport,
  records: RecordsListener = () => {},
  descriptors: DescriptorCache = new DescriptorCache(),
): typeof skeletonLoaderMachine {
  const publisher = recordPublisher(records);
  return skeletonLoaderMachine.provide({
    actors: {
      skeleton: fromPromise(({input, signal}) => {
        const lease = descriptors.lease();
        return transport
          .unary(
            `${SERVICE}${HostService.method.skeleton.name}`,
            toBinary(
              SkeletonRequestSchema,
              create(SkeletonRequestSchema, {runId: input, knownDescriptorsSha256: lease.known}),
            ),
            signal,
          )
          .then(bytes => {
            const response = lease.restore(fromBinary(SkeletonResponseSchema, bytes));
            return response;
          });
      }),
      failures: fromPromise<void, FailuresInput>(({input, signal}) =>
        pageFailures(transport, input, signal),
      ),
      tail: fromPromise<void, TailInput>(async ({input, signal}) => {
        const request = toBinary(
          TailRequestSchema,
          create(TailRequestSchema, {runId: input.runId, fromSeq: input.fromSeq}),
        );
        for await (const bytes of transport.stream(
          `${SERVICE}${HostService.method.tail.name}`,
          request,
          signal,
        )) {
          if (signal.aborted) return;
          const response = fromBinary(TailResponseSchema, bytes);
          publisher.tail(response);
          input.receive({
            type: 'history',
            records: response.records.map(bytes => fromBinary(RecordSchema, bytes)),
            through: response.throughSeq,
          });
          const fresh = unseen(response, input.cursor);
          if (fresh !== response) input.receive({type: 'window', through: response.throughSeq});
          if (fresh) input.receive({type: 'tail', response: fresh});
        }
      }),
    },
    actions: {
      publishSkeleton: (_, {response, load, valid}) => {
        if (valid && load !== undefined) publisher.skeleton(response, load);
      },
    },
  });
}

/** Publish immutable positions once across checkpoint materialization and its overlapping raw window. */
function recordPublisher(records: RecordsListener) {
  const checkpoint = new Set<bigint>();
  let through = 0n;
  return {
    skeleton(response: SkeletonResponse, load: SkeletonLoad): void {
      const facts = response.keyframe === undefined ? [] : checkpointRecords(load.skeleton);
      const initial = new Map<bigint, Uint8Array>();
      for (const bytes of [...facts, ...response.records]) {
        const seq = fromBinary(RecordSchema, bytes).seq;
        checkpoint.add(seq);
        initial.set(seq, bytes);
        if (seq > through) through = seq;
      }
      records(
        [...initial.entries()]
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
          .map(([, bytes]) => bytes),
      );
    },
    tail(response: TailResponse): void {
      records(
        response.records.filter(bytes => !checkpoint.delete(fromBinary(RecordSchema, bytes).seq)),
      );
      if (response.throughSeq >= through) checkpoint.clear();
    },
  };
}

/**
 * Error notices of the current epoch before `before`, newest page first, from bounded reads of
 * the log: each page a Tail from its start, cancelled once it reaches the page after. It stops at
 * the first page holding one or at the epoch's start.
 */
async function pageFailures(
  transport: RunTransport,
  input: FailuresInput,
  signal: AbortSignalLike,
): Promise<void> {
  let end = input.before;
  while (end !== undefined && end > input.epochStart) {
    const start = end - RECORD_WINDOW > input.epochStart ? end - RECORD_WINDOW : input.epochStart;
    const found = await readPage(transport, input, start, end, signal);
    if (found.length > 0) {
      input.receive({type: 'failures', records: found});
      return;
    }
    end = start;
  }
}

/** The current epoch's records in [start, end), by a Tail cancelled on reaching `end`. */
async function readPage(
  transport: RunTransport,
  input: FailuresInput,
  start: bigint,
  end: bigint,
  signal: AbortSignalLike,
): Promise<StoreRecord[]> {
  const found: StoreRecord[] = [];
  const request = toBinary(
    TailRequestSchema,
    create(TailRequestSchema, {runId: input.runId, fromSeq: start}),
  );
  for await (const bytes of transport.stream(
    `${SERVICE}${HostService.method.tail.name}`,
    request,
    signal,
  )) {
    if (signal.aborted) return found;
    const records = fromBinary(TailResponseSchema, bytes).records.map(item =>
      fromBinary(RecordSchema, item),
    );
    found.push(
      ...records.filter(
        record =>
          record.seq < end &&
          record.epoch === input.epoch &&
          record.body.case === 'notice' &&
          record.body.value.severity >= Severity.ERROR,
      ),
    );
    if (records.some(record => record.seq >= end)) break;
  }
  return found;
}

/** The part of a Tail message at or after the cursor, if any; the rest only feeds the record log. */
function unseen(response: TailResponse, cursor: bigint): TailResponse | undefined {
  const records = response.records;
  const start = records.findIndex(bytes => fromBinary(RecordSchema, bytes).seq >= cursor);
  if (start < 0) return undefined;
  if (start === 0) return response;
  return create(TailResponseSchema, {
    records: records.slice(start),
    head: response.head,
    throughSeq: response.throughSeq,
  });
}

/** The caller owns this actor; stop() aborts the unary/stream and cancels all deadlines. */
export function startSkeletonLoader(
  transport: RunTransport,
  schedule: ScheduleTimeout,
  input: SkeletonLoaderInput,
  records?: RecordsListener,
  descriptors?: DescriptorCache,
): Actor<typeof skeletonLoaderMachine> {
  if (!input.runId || !Number.isFinite(input.timeoutMs) || input.timeoutMs <= 0)
    throw new Error('loader: runId and positive timeoutMs required');
  return createActor(skeletonLoaderMachineFor(transport, records, descriptors), {
    input,
    clock: clockFrom(schedule),
  }).start();
}
