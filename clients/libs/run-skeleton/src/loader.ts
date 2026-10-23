import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HostService,
  RecordSchema,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  TailRequestSchema,
  type TailResponse,
  TailResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import {DescriptorCache} from './descriptors.js';
import {type SkeletonLoaderInput, skeletonLoaderMachine, type TailInput} from './loader-machine.js';

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
  return skeletonLoaderMachine.provide({
    actors: {
      skeleton: fromPromise(({input, signal}) =>
        transport
          .unary(
            `${SERVICE}${HostService.method.skeleton.name}`,
            toBinary(
              SkeletonRequestSchema,
              create(SkeletonRequestSchema, {
                runId: input,
                knownDescriptorsSha256: descriptors.known(),
              }),
            ),
            signal,
          )
          .then(bytes => {
            const response = descriptors.restore(fromBinary(SkeletonResponseSchema, bytes));
            records(response.records);
            return response;
          }),
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
          records(response.records);
          const fresh = unseen(response, input.cursor);
          if (fresh) input.receive({type: 'tail', response: fresh});
        }
      }),
    },
  });
}

/** The part of a Tail message at or after the cursor, if any; the rest only feeds the record log. */
function unseen(response: TailResponse, cursor: bigint): TailResponse | undefined {
  const records = response.records;
  const start = records.findIndex(bytes => fromBinary(RecordSchema, bytes).seq >= cursor);
  if (start < 0) return undefined;
  if (start === 0) return response;
  return create(TailResponseSchema, {
    records: records.slice(start),
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
