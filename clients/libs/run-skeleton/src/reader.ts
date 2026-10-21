/** Gateway read session effects, sharing the existing skeleton loader and immutable cache role. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {ScheduleTimeout} from '@vibesys/machine';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
} from '@vibesys/proto';
import type {AbortSignalLike} from '@vibesys/transport';
import {
  type RunTransport,
  type SkeletonCache,
  type SkeletonLoad,
  startSkeletonLoader,
} from './index.js';

export type ContentCache = Pick<SkeletonCache, 'getBlob' | 'putBlob'>;
export interface GatewayLogChunk {
  readonly text: string;
  readonly offset: number;
  readonly next: number;
}
export interface GatewayReader {
  watch(
    runId: string,
    receive: (load: SkeletonLoad | undefined, failed: boolean) => void,
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
  const decoder = new TextDecoder();
  let offset = 0n;
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
    receive({
      text: decoder.decode(response.data, {stream: true}),
      offset: Number(offset),
      next: Number(response.nextOffset),
    });
    offset = response.nextOffset;
  }
}

export function gatewayReader(
  transport: RunTransport,
  cache: ContentCache,
  schedule: ScheduleTimeout,
): GatewayReader {
  return {
    watch(runId, receive) {
      const loader = startSkeletonLoader(transport, schedule, {runId, timeoutMs: 30000});
      const subscription = loader.subscribe(snapshot =>
        receive(snapshot.context.load, snapshot.context.error !== undefined),
      );
      return () => {
        subscription.unsubscribe();
        loader.stop();
      };
    },
    content: (runId, hash, signal) => content(transport, cache, runId, hash, signal),
    log: (runId, streamId, signal, receive) => log(transport, runId, streamId, signal, receive),
  };
}
