/**
 * Faithful in-memory byte transport for the store reader, with explicit publication and faults, and
 * the host's say on a run's liveness as the reader is given it.
 */
import {create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  type Keyframe,
  KeyframeSchema,
  RecordSchema,
  RunHeadSchema,
  type RunLiveness,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
} from '@vibesys/proto';
import {type AbortSignalLike, registerAbort} from '@vibesys/transport';
import type {RunLivenessFeed, RunLivenessHint, RunLivenessState, RunTransport} from './index.js';

const SERVICE = `/${HostService.typeName}/`;
interface Subscriber {
  readonly method: string;
  readonly receive: () => void;
}

interface Cursor {
  nextSeq: bigint;
  streamId: string;
  offset: bigint;
}

/** What the host last said of a run, or that it lists the run no more. */
export type ObservedRun =
  | {
      readonly runId: string;
      readonly liveness: RunLiveness;
      readonly head?: MessageInitShape<typeof RunHeadSchema> | undefined;
    }
  | {readonly runId: string; readonly removed: true};

/**
 * The host as the reader sees it: delivering what it last said of each run, or not delivering. It
 * starts delivering nothing, which is a host with no runs; `lose` and `restore` are its stream
 * dropping and coming back, with the same runs.
 */
export class FakeRunHost {
  readonly #runs = new Map<string, RunLivenessHint>();
  readonly #feeds = new Set<{readonly runId: string; readonly notify: () => void}>();
  readonly #watchers = new Set<() => void>();
  #lost = false;
  /** How many feeds are subscribed now. */
  get watching(): number {
    return this.#feeds.size;
  }
  /** Resolves once something is subscribed to a run's liveness. */
  async watched(): Promise<void> {
    if (this.#feeds.size > 0) return;
    await new Promise<void>(resolve => {
      this.#watchers.add(resolve);
    });
  }
  observe(observation: ObservedRun): void {
    if ('removed' in observation) this.#runs.delete(observation.runId);
    else {
      const head =
        observation.head === undefined ? undefined : create(RunHeadSchema, observation.head);
      this.#runs.set(observation.runId, {
        epoch: head?.epoch ?? 0,
        value: observation.liveness,
        head,
      });
    }
    this.#changed();
  }
  /** The stream drops: the host says nothing, and what it said is no longer a fact. */
  lose(): void {
    this.#lost = true;
    this.#changed();
  }
  /** The stream is back and says what it said. */
  restore(): void {
    this.#lost = false;
    this.#changed();
  }
  /** The feed of one run, as `gatewayReader` takes it. */
  readonly feed = (runId: string): RunLivenessFeed => {
    let state: RunLivenessState = this.#stateOf(runId);
    return {
      subscribe: listener => {
        const entry = {
          runId,
          notify: () => {
            state = this.#stateOf(runId, state);
            listener();
          },
        };
        this.#feeds.add(entry);
        for (const resolve of this.#watchers) resolve();
        this.#watchers.clear();
        return () => {
          this.#feeds.delete(entry);
        };
      },
      getSnapshot: () => state,
    };
  };
  #stateOf(runId: string, previous?: RunLivenessState): RunLivenessState {
    if (this.#lost) return previous?.kind === 'lost' ? previous : {kind: 'lost'};
    const hint = this.#runs.get(runId);
    return previous?.kind === 'live' && previous.hint === hint ? previous : {kind: 'live', hint};
  }
  #changed(): void {
    for (const feed of [...this.#feeds]) feed.notify();
  }
}

// Match the gateway's Skeleton reserve and conservative Tail framing budget.
function boundedRecords(
  records: readonly StoreRecord[],
  kind: 'skeleton' | 'tail',
  reserved = 0,
): Uint8Array[] {
  const budget = (1 << 20) - (kind === 'skeleton' ? 32 : 16) - reserved;
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
  readonly #idle = new Set<() => void>();
  /** What the host says of each run's liveness, which the reader is given beside the transport. */
  readonly host = new FakeRunHost();
  readonly #closed = new Set<string>();
  readonly #records: StoreRecord[];
  readonly #keyframe: Uint8Array | undefined;
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
    keyframe?: Keyframe;
    blobs?: Readonly<Record<string, Uint8Array>>;
    streams?: Readonly<Record<string, Uint8Array>>;
    streamChunkBytes?: number;
  }) {
    this.#records = [...input.records];
    this.#keyframe =
      input.keyframe === undefined ? undefined : toBinary(KeyframeSchema, input.keyframe);
    this.#initialCount = input.initialCount ?? input.records.length;
    this.#blobs = input.blobs ?? {};
    this.#streams = input.streams ?? {};
    this.#streamChunkBytes = input.streamChunkBytes ?? 65536;
    if (!Number.isSafeInteger(this.#streamChunkBytes) || this.#streamChunkBytes < 1)
      throw new Error('streamChunkBytes must be a positive safe integer');
  }
  /** Resolves when every owned iterator has acknowledged cancellation or its end. */
  whenIdle(): Promise<void> {
    if (this.#subscribers.size === 0) return Promise.resolve();
    return new Promise(resolve => this.#idle.add(resolve));
  }
  get activeStreams(): number {
    return this.#subscribers.size;
  }
  publish(records: readonly StoreRecord[]): void {
    this.#records.push(...records);
    for (const subscriber of [...this.#subscribers]) subscriber.receive();
  }
  observe(observation: ObservedRun): void {
    this.host.observe(observation);
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
      return this.skeletonReply();
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
  private skeletonReply(): Uint8Array {
    const keyframe =
      this.#keyframe !== undefined && this.#keyframe.length + 32 < 1 << 20
        ? this.#keyframe
        : undefined;
    const through = keyframe === undefined ? -1n : fromBinary(KeyframeSchema, keyframe).throughSeq;
    const pending = this.#records
      .slice(0, this.#initialCount)
      .filter(record => record.seq > through);
    const records = boundedRecords(pending, 'skeleton', keyframe?.length ?? 0);
    const last = pending[records.length - 1];
    return toBinary(
      SkeletonResponseSchema,
      create(SkeletonResponseSchema, {
        ...(keyframe === undefined ? {} : {keyframe}),
        records,
        committedSeq: this.#records.at(-1)?.seq ?? 0n,
        nextSeq: last
          ? last.seq + 1n
          : keyframe === undefined
            ? (this.#records[0]?.seq ?? 1n)
            : through + 1n,
        complete: (last?.seq ?? through) === (this.#records.at(-1)?.seq ?? -1n),
      }),
    );
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
      if (this.#subscribers.size === 0) {
        for (const resolve of this.#idle) resolve();
        this.#idle.clear();
      }
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
  private batch(method: string, cursor: Cursor): Uint8Array | undefined {
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

export * from './sim-gateway.js';
