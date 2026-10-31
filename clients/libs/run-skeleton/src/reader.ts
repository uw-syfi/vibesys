/** Gateway read session effects, sharing the existing skeleton loader and immutable cache role. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {createActor, fromCallback} from 'xstate';
import {DescriptorCache} from './descriptors.js';
import {
  type LoadProgress,
  loadProgress,
  type RunLivenessFeed,
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
  /** The host is delivering. Its subscription holds the one lease of the run, not the reader. */
  readonly fresh: boolean;
}
export interface GatewayReader {
  /** One reader per run: history and records come from the same stream. */
  binary(
    runId: string,
    streamId: string,
    signal: AbortSignalLike,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void>;
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

async function binary(
  transport: RunTransport,
  runId: string,
  streamId: string,
  signal: AbortSignalLike,
  receive: (bytes: Uint8Array) => void,
  fromOffset = 0n,
): Promise<void> {
  if (fromOffset < 0n || fromOffset > BigInt(Number.MAX_SAFE_INTEGER))
    throw new Error('Stream offset exceeds screen precision');
  let offset = fromOffset;
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
    offset = response.nextOffset;
    receive(response.data);
  }
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
  let decodedOffset = Number(fromOffset);
  await binary(
    transport,
    runId,
    streamId,
    signal,
    data => {
      const text = decoder.decode(data, {stream: true});
      const next = decodedOffset + encoder.encode(text).length;
      if (text !== '') receive({text, offset: decodedOffset, next});
      decodedOffset = next;
    },
    fromOffset,
  );
  if (!signal.aborted) decoder.decode();
}

/** How long the history loader waits for a reply before it counts the attempt failed. */
const LOADER_TIMEOUT_MS = 30_000;

export function gatewayReader(
  transport: RunTransport,
  cache: ContentCache,
  schedule: ScheduleTimeout,
  /** What the host says of one run: its liveness, from the host's subscription. */
  liveness: (runId: string) => RunLivenessFeed,
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
            watch: fromCallback<ObservationEvent, ObservationOperation>(({input}) => {
              const feed = liveness(input.runId);
              const report = () => input.receive({type: 'liveness', state: feed.getSnapshot()});
              const release = feed.subscribe(report);
              // What the host already says reaches the reader after the watch has begun, as a
              // host that answers later would, so a reader never hears of it while it starts.
              queueMicrotask(report);
              return release;
            }),
          },
        }),
        {input: {runId, timeoutMs: LOADER_TIMEOUT_MS}, clock: clockFrom(schedule)},
      ).start();
      const subscription = actor.subscribe(snapshot => {
        const {context} = snapshot;
        receive({
          fresh: snapshot.matches({watching: 'live'}),
          load: context.load,
          progress: loadProgress(context.load),
          failure:
            context.failure ??
            (context.watchFailed && !isFinalLoad(context.load)
              ? {kind: 'transport', cause: 'liveness stream unavailable'}
              : undefined),
          terminal: context.terminal,
          hint: context.hint,
        });
      });
      return () => {
        subscription.unsubscribe();
        actor.stop();
      };
    },
    binary: (runId, streamId, signal, receive) =>
      binary(transport, runId, streamId, signal, receive),
    content: (runId, hash, signal) => content(transport, cache, runId, hash, signal),
    log: (runId, streamId, signal, receive, fromOffset) =>
      log(transport, runId, streamId, signal, receive, fromOffset),
  };
}
