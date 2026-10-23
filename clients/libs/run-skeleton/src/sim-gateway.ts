/**
 * A gateway Fake with a link model, for simulation tests of how a run opens. It answers Skeleton,
 * Tail, WatchRuns and GetBlobs as the Go gateway does (a keyframe that fits the reply budget, then
 * records up to the budget, then Tail for the rest), and delivers every reply over one shared pipe
 * with a round trip and a throughput, in the virtual time of the clock that drives the test.
 */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {FileDescriptorSetSchema} from '@bufbuild/protobuf/wkt';
import {sha256} from '@noble/hashes/sha2.js';
import {bytesToHex} from '@noble/hashes/utils.js';
import {
  GetBlobsResponseSchema,
  HostService,
  KeyframeSchema,
  RecordSchema,
  SkeletonRequestSchema,
  type SkeletonResponse,
  SkeletonResponseSchema,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {type AbortSignalLike, registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import type {RunTransport} from './loader.js';
import {foldSkeleton, seedSkeleton} from './model.js';

const SERVICE = `/${HostService.typeName}/`;
/** The gateway's reply budget; a Tail message is bounded by the same figure. */
export const GATEWAY_REPLY_BYTES = 1 << 20;

export interface LinkModel {
  readonly rttMs: number;
  readonly bytesPerMs: number;
}

/** What the host holds for one run and how its gateway behaves. */
export interface SimRun {
  readonly records: readonly StoreRecord[];
  /** Writer checkpoints: each is the number of leading records a keyframe covers. */
  readonly keyframes?: readonly number[];
  /** False models a run with no run.log: every read is NotFound. */
  readonly logPresent?: boolean;
  /** Reply budget; the gateway's 1 MiB by default. */
  readonly replyBytes?: number;
}

/** A fault the link injects into the n-th call of a method (0 is the first). */
export type SimFault =
  | {readonly kind: 'status'; readonly status: number}
  | {readonly kind: 'down'}
  | {readonly kind: 'cut'; readonly afterMessages: number; readonly clean: boolean};

export interface SimCall {
  readonly method: string;
  readonly request: Uint8Array;
  readonly issuedAt: number;
  readonly signal: AbortSignalLike;
}
export interface SimDelivery {
  readonly method: string;
  readonly bytes: number;
  readonly at: number;
}

/** A stored keyframe as the gateway sends it: descriptors named by hash, sets held apart. */
interface WireKeyframe {
  readonly bytes: Uint8Array;
  readonly sets: ReadonlyMap<string, Uint8Array>;
}

class Refused extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export interface SimClock {
  now(): number;
  readonly schedule: ScheduleTimeout;
}

export class SimGateway implements RunTransport {
  readonly calls: SimCall[] = [];
  readonly deliveries: SimDelivery[] = [];
  /** Every Skeleton reply sent, in order. */
  readonly skeletons: SkeletonResponse[] = [];
  readonly #published: StoreRecord[];
  readonly #waiting = new Set<() => void>();
  readonly #counts = new Map<string, number>();
  readonly #keyframes = new Map<number, WireKeyframe>();
  #busyUntil = 0;

  constructor(
    private readonly run: SimRun,
    private readonly clock: SimClock,
    private readonly link: LinkModel,
    private readonly fault: (method: string, ordinal: number) => SimFault | undefined = () =>
      undefined,
  ) {
    this.#published = [...run.records];
  }

  get replyBytes(): number {
    return this.run.replyBytes ?? GATEWAY_REPLY_BYTES;
  }

  /** Bytes delivered to the client by `at` (virtual ms) for the methods given, or all. */
  bytesBy(at: number, methods?: readonly string[]): number {
    return this.deliveries
      .filter(item => item.at <= at && (methods === undefined || methods.includes(item.method)))
      .reduce((sum, item) => sum + item.bytes, 0);
  }

  callsTo(method: string): readonly SimCall[] {
    return this.calls.filter(call => call.method === `${SERVICE}${method}`);
  }

  publish(records: readonly StoreRecord[]): void {
    this.#published.push(...records);
    for (const wake of [...this.#waiting]) wake();
  }

  async unary(method: string, request: Uint8Array, signal: AbortSignalLike): Promise<Uint8Array> {
    const call = this.begin(method, request, signal);
    if (call.fault?.kind === 'down') await this.arrive(method, 0, signal, true);
    this.refuse(call.fault);
    this.requireLog();
    const reply = this.unaryReply(method, request);
    await this.arrive(method, reply.length, signal);
    return reply;
  }

  async *stream(
    method: string,
    request: Uint8Array,
    signal: AbortSignalLike,
  ): AsyncIterable<Uint8Array> {
    const call = this.begin(method, request, signal);
    if (call.fault?.kind === 'down') await this.arrive(method, 0, signal, true);
    this.refuse(call.fault);
    if (method !== `${SERVICE}WatchRuns`) this.requireLog();
    const cut = call.fault?.kind === 'cut' ? call.fault : undefined;
    let sent = 0;
    if (method === `${SERVICE}WatchRuns`) {
      const hello = toBinary(
        WatchRunsResponseSchema,
        create(WatchRunsResponseSchema, {initialComplete: true}),
      );
      await this.arrive(method, hello.length, signal);
      yield hello;
      await new Promise<void>(resolve => {
        const registration = registerAbort(signal, resolve);
        if (registration.aborted) resolve();
      });
      return;
    }
    if (method !== `${SERVICE}Tail`) throw new Error(`Unknown stream method ${method}`);
    for await (const message of this.tail(request, signal)) {
      if (cut && sent >= cut.afterMessages) {
        if (cut.clean) return;
        throw new Error('connection lost');
      }
      await this.arrive(method, message.length, signal);
      sent += 1;
      yield message;
    }
  }

  private begin(method: string, request: Uint8Array, signal: AbortSignalLike) {
    const ordinal = this.#counts.get(method) ?? 0;
    this.#counts.set(method, ordinal + 1);
    this.calls.push({method, request: request.slice(), issuedAt: this.clock.now(), signal});
    if (signal.aborted) throw new Error('Cancelled');
    return {fault: this.fault(method, ordinal)};
  }

  private refuse(fault: SimFault | undefined): void {
    if (fault?.kind === 'status') throw new Refused('refused', fault.status);
  }

  private requireLog(): void {
    if (this.run.logPresent === false) throw new Refused('run.log is missing', 5);
  }

  /** The reply reaches the client after the shared pipe has carried it; `lost` never arrives. */
  private arrive(
    method: string,
    bytes: number,
    signal: AbortSignalLike,
    lost = false,
  ): Promise<void> {
    const now = this.clock.now();
    const start = Math.max(now + this.link.rttMs / 2, this.#busyUntil);
    const end = start + bytes / this.link.bytesPerMs;
    this.#busyUntil = end;
    const arrival = end + this.link.rttMs / 2;
    return new Promise<void>((resolve, reject) => {
      if (lost) {
        // A dead link: the caller's own deadline ends the wait.
        const registration = registerAbort(signal, () => reject(new Error('Cancelled')));
        if (registration.aborted) reject(new Error('Cancelled'));
        return;
      }
      const cancel = this.clock.schedule(() => {
        registration.detach();
        this.deliveries.push({method, bytes, at: arrival});
        resolve();
      }, arrival - now);
      const registration = registerAbort(signal, () => {
        cancel();
        reject(new Error('Cancelled'));
      });
      if (registration.aborted) {
        cancel();
        reject(new Error('Cancelled'));
      }
    });
  }

  private unaryReply(method: string, request: Uint8Array): Uint8Array {
    if (method === `${SERVICE}Skeleton`) {
      const asked = fromBinary(SkeletonRequestSchema, request);
      if (!asked.runId) throw new Error('run_id required');
      const skeleton = this.skeleton(new Set(asked.knownDescriptorsSha256));
      this.skeletons.push(skeleton);
      return toBinary(SkeletonResponseSchema, skeleton);
    }
    if (method === `${SERVICE}GetBlobs`)
      return toBinary(GetBlobsResponseSchema, create(GetBlobsResponseSchema, {}));
    throw new Error(`Unknown unary method ${method}`);
  }

  /** The latest checkpoint that fits the budget, then records until the budget is spent. */
  private skeleton(known: ReadonlySet<string>) {
    const records = this.run.records;
    const budget = this.replyBytes - 32;
    let covered = 0;
    let keyframe: Uint8Array | undefined;
    let sets: [string, Uint8Array][] = [];
    for (const candidate of [...(this.run.keyframes ?? [])].sort((a, b) => b - a)) {
      const wire = this.keyframe(candidate);
      const unknown = [...wire.sets].filter(([hash]) => !known.has(hash));
      const cost = wire.bytes.length + unknown.reduce((sum, [, data]) => sum + data.length + 80, 0);
      if (cost <= budget) {
        keyframe = wire.bytes;
        sets = unknown;
        covered = candidate;
        break;
      }
    }
    let size = (keyframe?.length ?? 0) + sets.reduce((sum, [, data]) => sum + data.length + 80, 0);
    const included: Uint8Array[] = [];
    for (const record of records.slice(covered)) {
      const bytes = toBinary(RecordSchema, record);
      if (size + bytes.length + 8 > budget) break;
      included.push(bytes);
      size += bytes.length + 8;
    }
    const last = records[covered + included.length - 1];
    const committed = records.at(-1)?.seq ?? 0n;
    return create(SkeletonResponseSchema, {
      ...(keyframe ? {keyframe} : {}),
      descriptorSets: sets.map(([hash, data]) => ({sha256: hash, data})),
      records: included,
      committedSeq: committed,
      nextSeq: last ? last.seq + 1n : (records[0]?.seq ?? 1n),
      complete: covered + included.length === records.length,
    });
  }

  private keyframe(covered: number): WireKeyframe {
    const cached = this.#keyframes.get(covered);
    if (cached) return cached;
    const records = this.run.records;
    const keyframe = create(KeyframeSchema, {
      ...foldSkeleton(seedSkeleton(), records.slice(0, covered)),
      throughSeq: records[covered - 1]?.seq ?? 0n,
    });
    const sets = new Map<string, Uint8Array>();
    for (const version of keyframe.epochs) {
      const descriptors = version.epoch?.descriptors;
      if (!version.epoch || !descriptors) continue;
      const data = toBinary(FileDescriptorSetSchema, descriptors);
      const hash = bytesToHex(sha256(data));
      sets.set(hash, data);
      version.descriptorsSha256 = hash;
      version.epoch.descriptors = undefined;
    }
    const wire = {bytes: toBinary(KeyframeSchema, keyframe), sets};
    this.#keyframes.set(covered, wire);
    return wire;
  }

  /** The log from the requested cursor in budget-sized messages, then whatever is published. */
  private async *tail(request: Uint8Array, signal: AbortSignalLike): AsyncGenerator<Uint8Array> {
    let next = fromBinary(TailRequestSchema, request).fromSeq;
    while (!signal.aborted) {
      const pending = this.#published.filter(record => record.seq >= next);
      const records: Uint8Array[] = [];
      let size = 16;
      for (const record of pending) {
        const bytes = toBinary(RecordSchema, record);
        if (size + bytes.length + 11 > this.replyBytes && records.length > 0) break;
        records.push(bytes);
        size += bytes.length + 11;
      }
      const last = pending[records.length - 1];
      if (last === undefined) {
        if (!(await this.untilPublished(signal))) return;
        continue;
      }
      next = last.seq + 1n;
      yield toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {records, throughSeq: last.seq}),
      );
    }
  }

  /** Resolves true when more is published, false when the caller leaves. */
  private untilPublished(signal: AbortSignalLike): Promise<boolean> {
    return new Promise(resolve => {
      const wake = (): void => {
        this.#waiting.delete(wake);
        registration.detach();
        resolve(true);
      };
      const registration = registerAbort(signal, () => {
        this.#waiting.delete(wake);
        resolve(false);
      });
      if (registration.aborted) resolve(false);
      else this.#waiting.add(wake);
    });
  }
}
