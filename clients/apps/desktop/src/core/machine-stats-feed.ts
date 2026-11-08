/**
 * The host's machine stats while a screen shows them: one `Subscribe` stream of the
 * `machine_stats` topic at a time, each frame read into an event (`readFrame`) and folded into
 * `MachineStats` (`state/machine-stats.ts`). The client checks every frame against its schema
 * before it gets here; a value of another topic is an invalid frame too.
 *
 * A stream is held to the bounds `subscribe.proto` sets, as gaps between the times its frames
 * arrived on the ambient clock (`nextLapse`). With no frame for the lease, or no word of the topic
 * for the first-frame bound while the snapshot is read, the stream is silent and a new one opens;
 * a reading not renewed within the age bound is marked stale and the stream is kept. The time the
 * host sampled at is never compared with this machine's.
 *
 * After a stream ends, a lost link goes to the connection and the next stream waits for the new
 * link; an end a new stream may cure opens one after a backoff; one only a new link may cure (a
 * host that cannot sample, a host that broke the contract) waits for the next link.
 */
import {
  type Absence,
  AbsenceReason,
  type HostService,
  type MachineStats as MachineStatsMessage,
  type SubscribeResponse,
  subscribeTiming,
  type TopicEntry,
  TopicFailureCode,
  topicPolicies,
} from '@vibesys/proto';
import * as Cause from 'effect/Cause';
import * as Clock from 'effect/Clock';
import type * as Context from 'effect/Context';
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import * as Queue from 'effect/Queue';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  type AbsenceCause,
  applyMachineEvent,
  emptyMachineStats,
  type GpuReading,
  type Lapse,
  type MachineBounds,
  type MachineEnd,
  type MachineEvent,
  type MachineSample,
  type MachineStats,
  type MemoryUse,
  machineRetry,
  nextLapse,
  openedAt,
  type Receipt,
  type Receipts,
  received,
  type Section,
  type TopicRefusal,
} from '../state/machine-stats.js';
import type {EffectClient} from './client/make-client.js';
import type {HostConnectionApi} from './connection.js';
import type {AppError} from './errors.js';
import {faultOf} from './fault.js';
import {defaultReconnect, failuresAfter, reconnectDelay} from './reconnect.js';

/** The typed client of the host's own service (beside the orchestrator's). */
type HostClient = EffectClient<typeof HostService.method>;

export interface MachineStatsFeed {
  /** Mount this while the stats are on screen; unmounting closes the stream and forgets them. */
  readonly watch: Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  /** What the streams brought; a screen reads it through `machineView`. */
  readonly model: Atom.Atom<MachineStats>;
}

/** What the feed is built from: `ambient` is the session's clock and randomness. */
export interface MachineStatsParts {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly ambient: Context.Context<never>;
}

interface Ports {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly model: SubscriptionRef.SubscriptionRef<MachineStats>;
}

const POLICY = topicPolicies.machineStats;

const BOUNDS: MachineBounds = {
  leaseMs: subscribeTiming.leaseMs,
  firstFrameMs: POLICY.firstFrameMs,
  staleAfterMs: POLICY.staleAfterMs,
};

/** Frames read off the call ahead of the fold; the call's own buffer is far larger. */
const AHEAD = 16;

/** Null for the reason the schema rejects before a frame gets here. */
const ABSENCE: Readonly<Record<AbsenceReason, AbsenceCause | null>> = {
  [AbsenceReason.UNSPECIFIED]: null,
  [AbsenceReason.TOOL_NOT_FOUND]: 'no-tool',
  [AbsenceReason.READ_FAILED]: 'read-failed',
  [AbsenceReason.TIMED_OUT]: 'timed-out',
  [AbsenceReason.PARSE_FAILED]: 'unparsed',
  [AbsenceReason.TOOL_UNVERIFIED]: 'unverified',
};

/** Null for the code the schema rejects before a frame gets here. */
const REFUSAL: Readonly<Record<TopicFailureCode, TopicRefusal | null>> = {
  [TopicFailureCode.UNSPECIFIED]: null,
  [TopicFailureCode.UNSUPPORTED]: 'unsupported',
  [TopicFailureCode.RESOURCE_EXHAUSTED]: 'over-limit',
  [TopicFailureCode.SOURCE_FAILED]: 'source-failed',
  [TopicFailureCode.INTERNAL]: 'internal',
};

/** A section of a sample: what was read, why nothing was, or null when the frame says neither. */
type Read<T> = Section<T> | null;

const absent = (absence: Absence): Read<never> => {
  const cause = ABSENCE[absence.reason];
  return cause === null ? null : {_tag: 'Absent', cause};
};

const bytes = (usedBytes: bigint, totalBytes: bigint): MemoryUse => ({
  usedBytes: Number(usedBytes),
  totalBytes: Number(totalBytes),
});

function cpuOf({cpu}: MachineStatsMessage): Read<number> {
  switch (cpu.case) {
    case 'cpuStats':
      return {_tag: 'Read', value: cpu.value.utilizationPercent};
    case 'cpuUnavailable':
      return absent(cpu.value);
    case undefined:
      return null;
    default:
      cpu satisfies never;
      return null;
  }
}

function memoryOf({memory}: MachineStatsMessage): Read<MemoryUse> {
  switch (memory.case) {
    case 'memoryStats':
      return {_tag: 'Read', value: bytes(memory.value.usedBytes, memory.value.totalBytes)};
    case 'memoryUnavailable':
      return absent(memory.value);
    case undefined:
      return null;
    default:
      memory satisfies never;
      return null;
  }
}

function gpusOf({gpu}: MachineStatsMessage): Read<readonly GpuReading[]> {
  switch (gpu.case) {
    case 'gpus':
      return {
        _tag: 'Read',
        value: gpu.value.gpus.map(one => ({
          index: one.index,
          name: one.name,
          utilizationPercent: one.utilizationPercent,
          memory: bytes(one.memoryUsedBytes, one.memoryTotalBytes),
        })),
      };
    case 'gpuUnavailable':
      return absent(gpu.value);
    case undefined:
      return null;
    default:
      gpu satisfies never;
      return null;
  }
}

/** The sample a value holds, or null when a section says neither a reading nor why it has none. */
function sampleOf(stats: MachineStatsMessage): MachineSample | null {
  const cpuPercent = cpuOf(stats);
  const memory = memoryOf(stats);
  const gpus = gpusOf(stats);
  return cpuPercent === null || memory === null || gpus === null
    ? null
    : {cpuPercent, memory, gpus};
}

/** What a frame means: an event to fold and what it renews, or how the stream ends. */
type Frame =
  | {readonly _tag: 'Next'; readonly event: MachineEvent | null; readonly receipt: Receipt}
  | {readonly _tag: 'End'; readonly why: MachineEnd};

const invalid = (detail: string): Frame => ({_tag: 'End', why: {_tag: 'Invalid', detail}});

function entryOf(entry: TopicEntry): Frame {
  const {value} = entry;
  switch (value.case) {
    case 'machineStats': {
      const sample = sampleOf(value.value);
      if (sample === null) return invalid('a section with neither a reading nor a reason');
      return {_tag: 'Next', event: {_tag: 'Entry', key: entry.key, sample}, receipt: 'value'};
    }
    case 'runs':
      return invalid('an entry of the runs topic');
    case undefined:
      return invalid('an entry with no value');
    default:
      value satisfies never;
      return invalid('an entry of an unknown topic');
  }
}

function readFrame({frame}: SubscribeResponse): Frame {
  switch (frame.case) {
    case 'entry':
      return entryOf(frame.value);
    case 'removed':
      return {_tag: 'Next', event: {_tag: 'Removed', key: frame.value}, receipt: 'data'};
    case 'synced':
      return {_tag: 'Next', event: {_tag: 'Synced'}, receipt: 'data'};
    case 'heartbeat':
      return {_tag: 'Next', event: null, receipt: 'heartbeat'};
    case 'failed': {
      const refusal = REFUSAL[frame.value.code];
      return refusal === null
        ? invalid('a failure with no code')
        : {_tag: 'End', why: {_tag: 'Refused', refusal}};
    }
    case undefined:
      return invalid('a frame with no content');
    default:
      frame satisfies never;
      return invalid('a frame of an unknown kind');
  }
}

/** How a stream ended, and when it synced (null if never): one that held earns back its backoff. */
interface Outcome {
  readonly end: MachineEnd;
  readonly syncedAt: number | null;
}

/** Where an open stream is: what the fold made of it, when its frames came, and when it synced. */
interface Course {
  readonly model: MachineStats;
  readonly receipts: Receipts;
  readonly syncedAt: number | null;
}

/** What one frame or one lapsed bound does to a stream: it goes on, or it ends. */
type Turn =
  | {readonly _tag: 'On'; readonly course: Course}
  | {readonly _tag: 'Over'; readonly outcome: Outcome};

const on = (course: Course): Turn => ({_tag: 'On', course});
const over = (end: MachineEnd, syncedAt: number | null): Turn => ({
  _tag: 'Over',
  outcome: {end, syncedAt},
});

const apply = (ports: Ports, event: MachineEvent) =>
  SubscriptionRef.updateAndGet(ports.model, model =>
    applyMachineEvent(model, event, POLICY.maxKeys),
  );

/** The end of a call that failed; a lost link is the connection's from here on. */
const failed = (ports: Ports, epoch: number, failure: AppError | Cause.Done) =>
  Effect.gen(function* () {
    if (Cause.isDone(failure)) return {_tag: 'Closed'} satisfies MachineEnd;
    const end = {_tag: 'Failed', fault: faultOf(failure)} satisfies MachineEnd;
    if (machineRetry(end) === 'lost') yield* ports.connection.suspect(epoch, failure);
    return end;
  });

/** A bound lapsed: a stream gone quiet is silent; a value gone unrenewed is only marked stale. */
const lapsed = (ports: Ports, course: Course, lapse: Lapse) =>
  Effect.gen(function* () {
    if (lapse !== 'value') return over({_tag: 'Silent'}, course.syncedAt);
    return on({...course, model: yield* apply(ports, {_tag: 'Aged'})});
  });

const heard = (ports: Ports, course: Course, response: SubscribeResponse) =>
  Effect.gen(function* () {
    // A frame is the host speaking: the host's freshness is as of now, whatever it says.
    yield* ports.connection.heard;
    const now = yield* Clock.currentTimeMillis;
    const frame = readFrame(response);
    if (frame._tag === 'End') return over(frame.why, course.syncedAt);
    const receipts = received(course.receipts, frame.receipt, now);
    if (frame.event === null) return on({...course, receipts});
    // A stream syncs once: the fold ignores a later `Synced`, and so does the time it held.
    const syncedAt = course.syncedAt ?? (frame.event._tag === 'Synced' ? now : null);
    const model = yield* apply(ports, frame.event);
    // The fold ends a stream that broke the bound on keys.
    if (model.ended !== null && !model.open) return over(model.ended, syncedAt);
    return on({model, receipts, syncedAt});
  });

/** Open one stream on the link of `epoch` and fold it until it ends or a bound ends it. */
const pass = (ports: Ports, epoch: number): Effect.Effect<Outcome> =>
  Effect.scoped(
    Effect.gen(function* () {
      const model = yield* apply(ports, {_tag: 'Open'});
      const frames = yield* Stream.toQueue(
        ports.client.subscribe({topic: {case: 'machineStats', value: {}}}),
        {capacity: AHEAD},
      );
      let course: Course = {
        model,
        receipts: openedAt(yield* Clock.currentTimeMillis),
        syncedAt: null,
      };
      while (true) {
        const {at, lapse} = nextLapse(course.model, course.receipts, BOUNDS);
        const wait = Duration.millis(Math.max(0, at - (yield* Clock.currentTimeMillis)));
        const taken = yield* Queue.take(frames).pipe(Effect.timeoutOption(wait), Effect.result);
        if (taken._tag === 'Failure') {
          return {end: yield* failed(ports, epoch, taken.failure), syncedAt: course.syncedAt};
        }
        const turn = Option.isNone(taken.success)
          ? yield* lapsed(ports, course, lapse)
          : yield* heard(ports, course, taken.success.value);
        if (turn._tag === 'Over') return turn.outcome;
        course = turn.course;
      }
    }),
  );

/** Follows the topic for as long as it is mounted: the caller interrupts it to unmount. */
const follow = (ports: Ports): Effect.Effect<never> =>
  Effect.gen(function* () {
    let after = -1;
    let failures = 0;
    while (true) {
      const ready = yield* ports.connection.ready(after).pipe(Effect.result);
      if (ready._tag === 'Failure') {
        // Nothing can open until the user retries the connection.
        yield* ports.connection.leftTerminal;
        continue;
      }
      const epoch = ready.success;
      const outcome = yield* pass(ports, epoch);
      yield* apply(ports, {_tag: 'End', why: outcome.end});
      if (machineRetry(outcome.end) !== 'backoff') {
        after = epoch;
        continue;
      }
      const syncedFor =
        outcome.syncedAt === null ? null : (yield* Clock.currentTimeMillis) - outcome.syncedAt;
      failures = failuresAfter(defaultReconnect, failures, syncedFor);
      yield* Effect.sleep(yield* reconnectDelay(defaultReconnect, failures));
      after = epoch - 1;
    }
  });

export const makeMachineStatsFeed = Effect.fn('MachineStatsFeed.make')(function* (
  parts: MachineStatsParts,
) {
  const model = yield* SubscriptionRef.make(emptyMachineStats);
  const ports: Ports = {client: parts.client, connection: parts.connection, model};
  const feed: MachineStatsFeed = {
    watch: Atom.make(
      follow(ports).pipe(
        Effect.provideContext(parts.ambient),
        // Unmounting forgets what was read: a screen opened later starts from a new snapshot.
        Effect.ensuring(apply(ports, {_tag: 'Stop'})),
      ),
    ),
    model: Atom.keepAlive(Atom.subscriptionRef(model)),
  };
  return feed;
});
