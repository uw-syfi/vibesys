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
  GetBlobsRequestSchema,
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

/**
 * How many checkpoints, newest first, a Skeleton reply tries before it falls back to replay. The
 * service's constant, pinned by `proto/testdata/skeleton-contract/cases.json`, which the service's
 * own tests and this simulation both run.
 */
export const GATEWAY_KEYFRAME_CANDIDATES = 4;
/** A stored checkpoint over this is not read by the service; it tries an older one. */
export const GATEWAY_MAX_STORED_KEYFRAME = 16 << 20;
/** What the service keeps back of the reply budget beside a keyframe, and beside the records. */
const KEYFRAME_RESERVE = 32;
const RECORDS_RESERVE = 24;

/** The checkpoint a reply carries: the newest of the first few that fit, or none. */
export function chooseKeyframe(
  through: readonly number[],
  fits: (through: number) => boolean,
): number | undefined {
  return [...through]
    .sort((a, b) => b - a)
    .slice(0, GATEWAY_KEYFRAME_CANDIDATES)
    .find(fits);
}

export interface LinkModel {
  readonly rttMs: number;
  readonly bytesPerMs: number;
}

/** What the host holds for one run and how its gateway behaves. */
export interface SimRun {
  readonly records: readonly StoreRecord[];
  /** Writer checkpoints: each is the number of leading records a keyframe covers. */
  readonly keyframes?: readonly number[];
  /**
   * What the host holds of the log: `present` (default); `pending`, a run directory whose run.log
   * its writer has not created yet (Skeleton answers an empty complete prefix, Tail waits for the
   * file); `unindexed`, a run the index does not know (every read is NotFound).
   */
  readonly log?: 'present' | 'pending' | 'unindexed';
  /** Reply budget; the gateway's 1 MiB by default. */
  readonly replyBytes?: number;
  /**
   * The size of a Tail message; the service's 1 MiB by default. A test lowers it to make a small
   * run arrive in several messages, which the service does for a large one.
   */
  readonly tailBytes?: number;
  /** A test scales the stored-checkpoint read limit down to its small runs. */
  readonly storedKeyframeLimit?: number;
  /** Checkpoints written before `Keyframe.failures_tracked`: no failure facts, flag false. */
  readonly legacyKeyframes?: boolean;
  /** Checkpoints that track record facts but predate failure facts. */
  readonly untrackedFailures?: boolean;
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
  /** What the checkpoint takes in the log, before its descriptors are named by hash. */
  readonly storedBytes: number;
  readonly bytes: Uint8Array;
  readonly sets: ReadonlyMap<string, Uint8Array>;
}

function varintLength(value: number): number {
  let length = 1;
  for (let rest = value; rest >= 128; rest = Math.floor(rest / 128)) length += 1;
  return length;
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

/** The service's WatchRuns heartbeat interval. */
const HEARTBEAT_MS = 5000;

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
    if (run.legacyKeyframes && run.untrackedFailures)
      throw new Error('a checkpoint cannot be legacy and failure-untracked at once');
    this.#published = run.log === 'pending' ? [] : [...run.records];
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
      // As the service does: a heartbeat every few seconds for as long as the stream is open.
      const beat = toBinary(
        WatchRunsResponseSchema,
        create(WatchRunsResponseSchema, {heartbeat: true}),
      );
      while (await this.sleep(HEARTBEAT_MS, signal)) {
        await this.arrive(method, beat.length, signal);
        yield beat;
      }
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

  /** Wait `ms` of virtual time: true when it passed, false when the stream was cancelled first. */
  private sleep(ms: number, signal: AbortSignalLike): Promise<boolean> {
    return new Promise<boolean>(resolve => {
      const cancel = this.clock.schedule(() => {
        registration.detach();
        resolve(true);
      }, ms);
      const registration = registerAbort(signal, () => {
        cancel();
        resolve(false);
      });
      if (registration.aborted) {
        cancel();
        resolve(false);
      }
    });
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
    if (this.run.log === 'unindexed') throw new Refused('run.log is missing', 5);
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
      // The service validates the request: at most 64 distinct names (InvalidArgument).
      if (
        asked.knownDescriptorsSha256.length > 64 ||
        new Set(asked.knownDescriptorsSha256).size !== asked.knownDescriptorsSha256.length
      )
        throw new Refused('known_descriptors_sha256: at most 64 distinct names', 3);
      const skeleton = this.skeleton(new Set(asked.knownDescriptorsSha256));
      this.skeletons.push(skeleton);
      return toBinary(SkeletonResponseSchema, skeleton);
    }
    if (method === `${SERVICE}GetBlobs`) {
      // The sim holds no blobs: each asked hash is answered missing, as the service answers it.
      const asked = fromBinary(GetBlobsRequestSchema, request);
      const blobs = asked.sha256.map(sha256 => ({sha256, errorCode: 5, errorMessage: 'Missing'}));
      return toBinary(GetBlobsResponseSchema, create(GetBlobsResponseSchema, {blobs}));
    }
    throw new Error(`Unknown unary method ${method}`);
  }

  /** As the service answers: a checkpoint that fits, then records until the budget is spent. */
  private skeleton(known: ReadonlySet<string>) {
    // A run directory with no run.log yet has nothing committed and nothing to replay.
    const records = this.run.log === 'pending' ? [] : this.run.records;
    const budget = this.replyBytes;
    const size = (keyframe?: Uint8Array, sets: readonly [string, Uint8Array][] = []): number =>
      toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {
          ...(keyframe ? {keyframe} : {}),
          descriptorSets: sets.map(([hash, data]) => ({sha256: hash, data})),
        }),
      ).length;
    const unknownSets = (covered: number) =>
      [...this.keyframe(covered).sets].filter(([hash]) => !known.has(hash));
    const covered =
      chooseKeyframe(this.run.keyframes ?? [], through =>
        through <= records.length &&
        this.keyframe(through).storedBytes <=
          (this.run.storedKeyframeLimit ?? GATEWAY_MAX_STORED_KEYFRAME)
          ? size(this.keyframe(through).bytes, unknownSets(through)) + KEYFRAME_RESERVE <= budget
          : false,
      ) ?? 0;
    const keyframe = covered > 0 ? this.keyframe(covered).bytes : undefined;
    const sets = covered > 0 ? unknownSets(covered) : [];
    let used = size(keyframe, sets) + RECORDS_RESERVE;
    const included: Uint8Array[] = [];
    for (const record of records.slice(covered)) {
      const bytes = toBinary(RecordSchema, record);
      const cost = 1 + varintLength(bytes.length) + bytes.length;
      if (used + cost > budget) break;
      included.push(bytes);
      used += cost;
    }
    const last = records[covered + included.length - 1];
    return create(SkeletonResponseSchema, {
      ...(keyframe ? {keyframe} : {}),
      descriptorSets: sets.map(([hash, data]) => ({sha256: hash, data})),
      records: included,
      committedSeq: records.at(-1)?.seq ?? 0n,
      nextSeq: last ? last.seq + 1n : (records[0]?.seq ?? 0n),
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
    if (this.run.legacyKeyframes) {
      keyframe.failures = [];
      keyframe.failuresTracked = false;
      keyframe.recordsTracked = false;
      keyframe.intents = [];
      keyframe.recordFacts = [];
      for (const version of keyframe.epochs) {
        version.startsEpoch = false;
        version.startedAt = undefined;
      }
    } else if (this.run.untrackedFailures) {
      keyframe.failures = [];
      keyframe.failuresTracked = false;
    }
    const storedBytes = toBinary(KeyframeSchema, keyframe).length;
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
    const wire = {storedBytes, bytes: toBinary(KeyframeSchema, keyframe), sets};
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
        if (
          size + bytes.length + 11 > (this.run.tailBytes ?? GATEWAY_REPLY_BYTES) &&
          records.length > 0
        )
          break;
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
