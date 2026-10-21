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
} from '@vibesys/proto';
import {type AbortSignalLike, registerAbort} from '@vibesys/transport';
import type {RunTransport} from './index.js';

const SERVICE = `/${HostService.typeName}/`;
interface Subscriber {
  readonly method: string;
  readonly receive: () => void;
}

export class FakeRunTransport implements RunTransport {
  readonly calls: {method: string; request: Uint8Array; signal: AbortSignalLike}[] = [];
  readonly #subscribers = new Set<Subscriber>();
  readonly #records: StoreRecord[];
  #initialCount: number;
  readonly #blobs: Readonly<Record<string, Uint8Array>>;
  readonly #streams: Readonly<Record<string, Uint8Array>>;
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
  }) {
    this.#records = [...input.records];
    this.#initialCount = input.initialCount ?? input.records.length;
    this.#blobs = input.blobs ?? {};
    this.#streams = input.streams ?? {};
  }
  get activeStreams(): number {
    return this.#subscribers.size;
  }
  publish(records: readonly StoreRecord[]): void {
    this.#records.push(...records);
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
      const records = this.#records.slice(0, this.#initialCount);
      return toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {
          records: records.map(record => toBinary(RecordSchema, record)),
          committedSeq: this.#records.at(-1)?.seq ?? 0n,
          nextSeq: (records.at(-1)?.seq ?? 0n) + 1n,
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
        const batch = this.batch(method, cursor);
        if (batch) {
          yield batch;
          continue;
        }
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
    } else throw new Error(`Unknown stream method ${method}`);
    return {nextSeq, streamId, offset};
  }
  private batch(
    method: string,
    cursor: {nextSeq: bigint; streamId: string; offset: bigint},
  ): Uint8Array | undefined {
    if (method === `${SERVICE}Tail`) {
      const records = this.#records.filter(record => record.seq >= cursor.nextSeq);
      if (records.length > 0) {
        const throughSeq = records.at(-1)?.seq ?? 0n;
        cursor.nextSeq = throughSeq + 1n;
        return toBinary(
          TailResponseSchema,
          create(TailResponseSchema, {
            records: records.map(record => toBinary(RecordSchema, record)),
            throughSeq,
          }),
        );
      }
    } else {
      const content = this.#streams[cursor.streamId];
      if (!content) throw new Error('Missing stream');
      if (cursor.offset < BigInt(content.length)) {
        const data = content.slice(Number(cursor.offset));
        cursor.offset = BigInt(content.length);
        return toBinary(
          TailStreamResponseSchema,
          create(TailStreamResponseSchema, {data, nextOffset: cursor.offset}),
        );
      }
    }
    return undefined;
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
