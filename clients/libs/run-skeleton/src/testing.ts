/** Faithful in-memory byte transport for the store reader, with explicit publication and faults. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  RecordSchema,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
  type WatchRunsResponse,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {type AbortSignalLike, registerAbort} from '@vibesys/transport';
import type {RunTransport} from './index.js';

const SERVICE = `/${HostService.typeName}/`;
interface Subscriber {
  readonly method: string;
  readonly receive: () => void;
}

interface Cursor {
  nextSeq: bigint;
  streamId: string;
  offset: bigint;
  observation: number;
  initial: WatchRunsResponse[];
}

// Match the gateway's Skeleton reserve and conservative Tail framing budget.
function boundedRecords(records: readonly StoreRecord[], kind: 'skeleton' | 'tail'): Uint8Array[] {
  const budget = (1 << 20) - (kind === 'skeleton' ? 32 : 16);
  const batch: Uint8Array[] = [];
  let size = 0;
  for (const record of records) {
    const bytes = toBinary(RecordSchema, record);
    const cost =
      kind === 'skeleton'
        ? toBinary(SkeletonResponseSchema, create(SkeletonResponseSchema, {records: [bytes]}))
            .length
        : bytes.length + 11;
    if (size + cost > budget && (kind === 'skeleton' || batch.length > 0)) break;
    batch.push(bytes);
    size += cost;
  }
  return batch;
}

export class FakeRunTransport implements RunTransport {
  readonly calls: {method: string; request: Uint8Array; signal: AbortSignalLike}[] = [];
  readonly #subscribers = new Set<Subscriber>();
  readonly #observations: WatchRunsResponse[] = [];
  readonly #closed = new Set<string>();
  readonly #records: StoreRecord[];
  #initialCount: number;
  readonly #blobs: Readonly<Record<string, Uint8Array>>;
  readonly #streams: Readonly<Record<string, Uint8Array>>;
  readonly #streamChunkBytes: number;
  readonly #started = new Map<string, Set<() => void>>();
  async called(method: string): Promise<void> {
    if (this.calls.some(call => call.method === method)) return;
    await new Promise<void>(resolve => {
      const waiting = this.#started.get(method) ?? new Set();
      this.#started.set(method, waiting);
      waiting.add(resolve);
    });
  }
  readonly held = new Set<string>();
  readonly #releases = new Map<string, Set<() => void>>();
  release(method: string): void {
    this.held.delete(method);
    for (const finish of this.#releases.get(method) ?? []) finish();
  }
  readonly failures = new Map<string, Error>();

  constructor(input: {
    records: readonly StoreRecord[];
    initialCount?: number;
    blobs?: Readonly<Record<string, Uint8Array>>;
    streams?: Readonly<Record<string, Uint8Array>>;
    streamChunkBytes?: number;
  }) {
    this.#records = [...input.records];
    this.#initialCount = input.initialCount ?? input.records.length;
    this.#blobs = input.blobs ?? {};
    this.#streams = input.streams ?? {};
    this.#streamChunkBytes = input.streamChunkBytes ?? 65536;
    if (!Number.isSafeInteger(this.#streamChunkBytes) || this.#streamChunkBytes < 1)
      throw new Error('streamChunkBytes must be a positive safe integer');
  }
  get activeStreams(): number {
    return this.#subscribers.size;
  }
  publish(records: readonly StoreRecord[]): void {
    this.#records.push(...records);
    for (const subscriber of [...this.#subscribers]) subscriber.receive();
  }
  observe(response: WatchRunsResponse): void {
    this.#observations.push(response);
    for (const subscriber of [...this.#subscribers]) subscriber.receive();
  }
  close(method: string): void {
    this.#closed.add(method);
    for (const subscriber of [...this.#subscribers]) subscriber.receive();
  }
  recover(method: string): void {
    this.#closed.delete(method);
    this.failures.delete(method);
  }
  fail(method: string, error: Error): void {
    this.failures.set(method, error);
    for (const subscriber of [...this.#subscribers]) subscriber.receive();
  }
  async unary(method: string, request: Uint8Array, signal: AbortSignalLike): Promise<Uint8Array> {
    this.calls.push({method, request: request.slice(), signal});
    for (const notify of this.#started.get(method) ?? []) notify();
    this.#started.delete(method);
    if (signal.aborted) throw new Error('Cancelled');
    await this.wait(method, signal);
    const failure = this.failures.get(method);
    if (failure) throw failure;
    if (method === `${SERVICE}Skeleton`) {
      const input = fromBinary(SkeletonRequestSchema, request);
      if (!input.runId) throw new Error('run_id required');
      const records = boundedRecords(this.#records.slice(0, this.#initialCount), 'skeleton');
      const last = this.#records[records.length - 1];
      return toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {
          records,
          committedSeq: this.#records.at(-1)?.seq ?? 0n,
          nextSeq: last ? last.seq + 1n : (this.#records[0]?.seq ?? 1n),
          complete: records.length === this.#records.length,
        }),
      );
    }
    if (method === `${SERVICE}GetBlobs`) {
      const input = fromBinary(GetBlobsRequestSchema, request);
      if (!input.runId) throw new Error('run_id required');
      return toBinary(
        GetBlobsResponseSchema,
        create(GetBlobsResponseSchema, {
          blobs: input.sha256.map(sha256 => {
            const data = this.#blobs[sha256];
            return data === undefined
              ? {sha256, errorCode: 5, errorMessage: 'Missing'}
              : {sha256, data};
          }),
        }),
      );
    }
    throw new Error(`Unknown unary method ${method}`);
  }
  async *stream(
    method: string,
    request: Uint8Array,
    signal: AbortSignalLike,
  ): AsyncIterable<Uint8Array> {
    this.calls.push({method, request: request.slice(), signal});
    for (const notify of this.#started.get(method) ?? []) notify();
    this.#started.delete(method);
    if (signal.aborted) return;
    await this.wait(method, signal);
    const failure = this.failures.get(method);
    if (failure) throw failure;
    const cursor = this.cursor(method, request);
    let wake: (() => void) | undefined;
    const subscriber = {method, receive: () => wake?.()};
    this.#subscribers.add(subscriber);
    const registration = registerAbort(signal, subscriber.receive);
    try {
      while (!signal.aborted) {
        const failure = this.failures.get(method);
        if (failure) throw failure;
        const batch = this.batch(method, cursor);
        if (batch) {
          yield batch;
          continue;
        }
        if (this.#closed.has(method)) return;
        await new Promise<void>(resolve => {
          wake = resolve;
        });
        wake = undefined;
      }
    } finally {
      registration.detach();
      this.#subscribers.delete(subscriber);
    }
  }
  private cursor(method: string, request: Uint8Array) {
    let nextSeq = 0n;
    let streamId = '';
    let offset = 0n;
    if (method === `${SERVICE}Tail`) {
      const input = fromBinary(TailRequestSchema, request);
      if (!input.runId) throw new Error('run_id required');
      nextSeq = input.fromSeq;
    } else if (method === `${SERVICE}TailStream`) {
      const input = fromBinary(TailStreamRequestSchema, request);
      if (!input.runId || !input.streamId) throw new Error('run_id and stream_id required');
      streamId = input.streamId;
      offset = input.fromOffset;
    } else if (method !== `${SERVICE}WatchRuns`) throw new Error(`Unknown stream method ${method}`);
    const current = new Map<string, WatchRunsResponse>();
    for (const response of this.#observations) {
      if (response.removed) current.delete(response.runId);
      else current.set(response.runId, response);
    }
    return {
      nextSeq,
      streamId,
      offset,
      observation: this.#observations.length,
      initial: [...current.values(), create(WatchRunsResponseSchema, {initialComplete: true})],
    };
  }
  private batch(method: string, cursor: Cursor): Uint8Array | undefined {
    if (method === `${SERVICE}WatchRuns`) {
      return this.observation(cursor);
    }
    if (method === `${SERVICE}Tail`) {
      const pending = this.#records.filter(record => record.seq >= cursor.nextSeq);
      const records = boundedRecords(pending, 'tail');
      if (records.length > 0) {
        const throughSeq = pending[records.length - 1]?.seq ?? 0n;
        cursor.nextSeq = throughSeq + 1n;
        return toBinary(
          TailResponseSchema,
          create(TailResponseSchema, {
            records,
            throughSeq,
          }),
        );
      }
    } else {
      const content = this.#streams[cursor.streamId];
      if (!content) throw new Error('Missing stream');
      if (cursor.offset < BigInt(content.length)) {
        const data = content.slice(
          Number(cursor.offset),
          Number(cursor.offset) + this.#streamChunkBytes,
        );
        cursor.offset += BigInt(data.length);
        return toBinary(
          TailStreamResponseSchema,
          create(TailStreamResponseSchema, {data, nextOffset: cursor.offset}),
        );
      }
    }
    return undefined;
  }
  private observation(cursor: Cursor): Uint8Array | undefined {
    const initial = cursor.initial.shift();
    if (initial) return toBinary(WatchRunsResponseSchema, initial);
    const response = this.#observations[cursor.observation];
    if (response) cursor.observation += 1;
    return response ? toBinary(WatchRunsResponseSchema, response) : undefined;
  }
  private async wait(method: string, signal: AbortSignalLike): Promise<void> {
    if (!this.held.has(method)) return;
    await new Promise<void>((resolve, reject) => {
      const waiting = this.#releases.get(method) ?? new Set<() => void>();
      this.#releases.set(method, waiting);
      const finish = (): void => {
        waiting.delete(finish);
        registration.detach();
        resolve();
      };
      const abort = (): void => {
        waiting.delete(finish);
        registration.detach();
        reject(new Error('Cancelled'));
      };
      const registration = registerAbort(signal, abort);
      waiting.add(finish);
      if (registration.aborted) abort();
    });
  }
}
