/** Gateway read session effects, sharing the existing skeleton loader and immutable cache role. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  TailRequestSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
  WatchRunsRequestSchema,
  type WatchRunsResponse,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {
  type RunLivenessHint,
  type RunTransport,
  type SkeletonCache,
  type SkeletonLoad,
  startSkeletonLoader,
} from './index.js';

import {isFinalLoad} from './loading.js';
import {
  type ObservationEvent,
  type ObservationOperation,
  runObservationMachine,
} from './observation.js';

export type ContentCache = Pick<SkeletonCache, 'getBlob' | 'putBlob'>;
export interface GatewayLogChunk {
  readonly text: string;
  readonly offset: number;
  readonly next: number;
}
export interface GatewayReader {
  records(
    runId: string,
    fromSeq: bigint,
    signal: AbortSignalLike,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void>;
  watch(
    runId: string,
    receive: (load: SkeletonLoad | undefined, failed: boolean, hint?: RunLivenessHint) => void,
  ): () => void;
  content(
    runId: string,
    hash: string,
    signal: AbortSignalLike,
  ): Promise<{hash: string; text: string}>;
  log(
    runId: string,
    streamId: string,
    signal: AbortSignalLike,
    receive: (chunk: GatewayLogChunk) => void,
  ): Promise<void>;
}
const SERVICE = `/${HostService.typeName}/`;

async function content(
  transport: RunTransport,
  cache: ContentCache,
  runId: string,
  hash: string,
  signal: AbortSignalLike,
) {
  let bytes = await cache.getBlob(hash);
  if (signal.aborted) throw new Error('Cancelled');
  if (bytes === undefined) {
    const response = fromBinary(
      GetBlobsResponseSchema,
      await transport.unary(
        `${SERVICE}${HostService.method.getBlobs.name}`,
        toBinary(GetBlobsRequestSchema, create(GetBlobsRequestSchema, {runId, sha256: [hash]})),
        signal,
      ),
    );
    const blob = response.blobs[0];
    if (response.blobs.length !== 1 || !blob || blob.sha256 !== hash || blob.errorCode !== 0)
      throw new Error('Blob unavailable');
    if (signal.aborted) throw new Error('Cancelled');
    await cache.putBlob(hash, blob.data);
    bytes = blob.data;
  }
  return {hash, text: new TextDecoder('utf-8', {fatal: true}).decode(bytes)};
}

async function log(
  transport: RunTransport,
  runId: string,
  streamId: string,
  signal: AbortSignalLike,
  receive: (chunk: GatewayLogChunk) => void,
): Promise<void> {
  const decoder = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true});
  const encoder = new TextEncoder();
  let offset = 0n;
  let decodedOffset = 0;
  for await (const bytes of transport.stream(
    `${SERVICE}${HostService.method.tailStream.name}`,
    toBinary(TailStreamRequestSchema, create(TailStreamRequestSchema, {runId, streamId})),
    signal,
  )) {
    if (signal.aborted) return;
    const response = fromBinary(TailStreamResponseSchema, bytes);
    if (
      response.nextOffset !== offset + BigInt(response.data.length) ||
      response.nextOffset > BigInt(Number.MAX_SAFE_INTEGER)
    )
      throw new Error('Stream offset mismatch');
    const text = decoder.decode(response.data, {stream: true});
    const next = decodedOffset + encoder.encode(text).length;
    if (text !== '') receive({text, offset: decodedOffset, next});
    decodedOffset = next;
    offset = response.nextOffset;
  }
  if (!signal.aborted) decoder.decode();
}

export function gatewayReader(
  transport: RunTransport,
  cache: ContentCache,
  schedule: ScheduleTimeout,
): GatewayReader {
  return {
    async records(runId, fromSeq, signal, receive) {
      for await (const bytes of transport.stream(
        `${SERVICE}${HostService.method.tail.name}`,
        toBinary(TailRequestSchema, create(TailRequestSchema, {runId, fromSeq})),
        signal,
      )) {
        if (signal.aborted) return;
        receive(bytes);
      }
    },
    watch(runId, receive) {
      const actor = createActor(
        runObservationMachine.provide({
          actors: {
            history: fromCallback<ObservationEvent, ObservationOperation>(({input}) => {
              const loader = startSkeletonLoader(transport, schedule, input);
              const subscription = loader.subscribe(snapshot =>
                input.receive({
                  type: 'read',
                  load: snapshot.context.load,
                  failed: snapshot.context.error !== undefined,
                }),
              );
              return () => {
                subscription.unsubscribe();
                loader.stop();
              };
            }),
            watch: fromPromise<void, ObservationOperation>(async ({input, signal}) => {
              for await (const bytes of transport.stream(
                `${SERVICE}${HostService.method.watchRuns.name}`,
                toBinary(WatchRunsRequestSchema, create(WatchRunsRequestSchema)),
                signal,
              )) {
                if (signal.aborted) return;
                const response = fromBinary(WatchRunsResponseSchema, bytes);
                input.receive({type: 'ready'});
                if (response.runId !== input.runId) continue;
                input.receive({
                  type: 'hint',
                  hint: response.removed
                    ? undefined
                    : {
                        epoch: observedEpoch(response),
                        value: response.liveness,
                        head: response.head,
                      },
                });
              }
            }),
          },
        }),
        {input: {runId, timeoutMs: 30000}, clock: clockFrom(schedule)},
      ).start();
      const subscription = actor.subscribe(({context}) =>
        receive(
          context.load,
          context.failed || (context.watchFailed && !isFinalLoad(context.load)),
          context.hint,
        ),
      );
      return () => {
        subscription.unsubscribe();
        actor.stop();
      };
    },
    content: (runId, hash, signal) => content(transport, cache, runId, hash, signal),
    log: (runId, streamId, signal, receive) => log(transport, runId, streamId, signal, receive),
  };
}

function observedEpoch(response: WatchRunsResponse): number {
  return response.head?.epoch ?? 0;
}
