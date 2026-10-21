import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  HostService,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import {type SkeletonLoaderInput, skeletonLoaderMachine, type TailInput} from './loader-machine.js';

/** Generic unary/server-stream byte bridge. Both operations must release resources on abort. */
export interface RunTransport {
  unary(method: string, request: Uint8Array, signal: AbortSignalLike): Promise<Uint8Array>;
  stream(method: string, request: Uint8Array, signal: AbortSignalLike): AsyncIterable<Uint8Array>;
}
const SERVICE = `/${HostService.typeName}/`;

export function skeletonLoaderMachineFor(transport: RunTransport): typeof skeletonLoaderMachine {
  return skeletonLoaderMachine.provide({
    actors: {
      skeleton: fromPromise(({input, signal}) =>
        transport
          .unary(
            `${SERVICE}${HostService.method.skeleton.name}`,
            toBinary(SkeletonRequestSchema, create(SkeletonRequestSchema, {runId: input})),
            signal,
          )
          .then(bytes => fromBinary(SkeletonResponseSchema, bytes)),
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
          input.receive({type: 'tail', response: fromBinary(TailResponseSchema, bytes)});
        }
      }),
    },
  });
}

/** The caller owns this actor; stop() aborts the unary/stream and cancels all deadlines. */
export function startSkeletonLoader(
  transport: RunTransport,
  schedule: ScheduleTimeout,
  input: SkeletonLoaderInput,
): Actor<typeof skeletonLoaderMachine> {
  if (!input.runId || !Number.isFinite(input.timeoutMs) || input.timeoutMs <= 0)
    throw new Error('loader: runId and positive timeoutMs required');
  return createActor(skeletonLoaderMachineFor(transport), {
    input,
    clock: clockFrom(schedule),
  }).start();
}
