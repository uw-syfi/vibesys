/** Gateway read session effects, sharing the existing skeleton loader and immutable cache role. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
  WatchRunsRequestSchema,
  type WatchRunsResponse,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {DescriptorCache} from './descriptors.js';
import {
  type LoadProgress,
  loadProgress,
  type RunLivenessHint,
  type RunTransport,
  type SkeletonCache,
  type SkeletonLoad,
  type SkeletonLoadFailure,
  startSkeletonLoader,
} from './index.js';
import type {RecordsListener} from './loader.js';

export type {RecordsListener};

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
/** What a reader knows of one run's history and liveness; each field is a fact, not a message. */
export interface GatewayObservation {
  readonly load: SkeletonLoad | undefined;
  readonly progress: LoadProgress;
  /** Set while the history is not being read: a refusal, or a failure being retried. */
  readonly failure: SkeletonLoadFailure | undefined;
  /** The reader gave up on this failure; a new observation is the only retry. */
  readonly terminal: boolean;
  readonly hint: RunLivenessHint | undefined;
}
export interface GatewayReader {
  /** One reader per run: history and records come from the same stream. */
  watch(
    runId: string,
    receive: (observation: GatewayObservation) => void,
    records: RecordsListener,
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
    fromOffset?: bigint,
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
  fromOffset = 0n,
): Promise<void> {
  if (fromOffset < 0n || fromOffset > BigInt(Number.MAX_SAFE_INTEGER))
    throw new Error('Stream offset exceeds screen precision');
  const decoder = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true});
  const encoder = new TextEncoder();
  let offset = fromOffset;
  let decodedOffset = Number(fromOffset);
  for await (const bytes of transport.stream(
    `${SERVICE}${HostService.method.tailStream.name}`,
    toBinary(
      TailStreamRequestSchema,
      create(TailStreamRequestSchema, {runId, streamId, fromOffset}),
    ),
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
  const descriptors = new DescriptorCache();
  return {
    watch(runId, receive, records) {
      const actor = createActor(
        runObservationMachine.provide({
          actors: {
            history: fromCallback<ObservationEvent, ObservationOperation>(({input}) => {
              const loader = startSkeletonLoader(transport, schedule, input, records, descriptors);
              const subscription = loader.subscribe(snapshot =>
                input.receive({
                  type: 'read',
                  load: snapshot.context.load,
                  failure: snapshot.context.error,
                  terminal: snapshot.matches('failed'),
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
        receive({
          load: context.load,
          progress: loadProgress(context.load),
          failure:
            context.failure ??
            (context.watchFailed && !isFinalLoad(context.load)
              ? {kind: 'transport', cause: 'liveness stream unavailable'}
              : undefined),
          terminal: context.terminal,
          hint: context.hint,
        }),
      );
      return () => {
        subscription.unsubscribe();
        actor.stop();
      };
    },
    content: (runId, hash, signal) => content(transport, cache, runId, hash, signal),
    log: (runId, streamId, signal, receive, fromOffset) =>
      log(transport, runId, streamId, signal, receive, fromOffset),
  };
}

function observedEpoch(response: WatchRunsResponse): number {
  // Phase 1 heads omit the canonical epoch (proto3 decodes it as zero).
  // Preserve their process epoch for fencing; liveness still comes from the gateway's lock probe.
  return response.head?.epoch || response.head?.process?.epoch || 0;
}
