/**
 * The one delivery loop for every keyed latest-value HostService.Subscribe topic. A descriptor
 * supplies only its request, value arm and fold events; this module owns leases, snapshot bounds,
 * value age, typed ends, retry/backoff, connection suspicion and interruption cleanup.
 */
import {
  type HostService,
  type SubscribeResponse,
  subscribeTiming,
  type TopicEntry,
  TopicFailureCode,
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
import type {TopicEnd, TopicRefusal} from '../state/topic-delivery.js';
import {topicRetry} from '../state/topic-delivery.js';
import type {EffectClient} from './client/make-client.js';
import type {HostConnectionApi} from './connection.js';
import type {AppError} from './errors.js';
import {faultOf} from './fault.js';
import {defaultReconnect, failuresAfter, reconnectDelay} from './reconnect.js';

type HostClient = EffectClient<typeof HostService.method>;
type SubscribeRequest = Parameters<HostClient['subscribe']>[0];

interface KeyedTopicPolicy {
  readonly firstFrameMs: number;
  readonly staleAfterMs: number;
}

type Entry<E> = {readonly event: E} | {readonly invalid: string};

/** The topic-specific seams around the shared delivery lifecycle. */
export interface KeyedTopicDescriptor<S, E> {
  readonly request: SubscribeRequest;
  readonly policy: KeyedTopicPolicy;
  readonly open: E;
  readonly synced: E;
  readonly aged: E | null;
  readonly stop: E;
  readonly entry: (entry: TopicEntry) => Entry<E>;
  readonly removed: (key: string) => E;
  readonly ended: (end: TopicEnd) => E;
  readonly apply: (event: E) => Effect.Effect<S>;
  /** A fold-detected contract violation, such as too many keys. */
  readonly invalid: (state: S) => TopicEnd | null;
}

export interface KeyedTopicParts {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly ambient: Context.Context<never>;
}

const AHEAD = 32;

const REFUSAL: Readonly<Record<TopicFailureCode, TopicRefusal | null>> = {
  [TopicFailureCode.UNSPECIFIED]: null,
  [TopicFailureCode.UNSUPPORTED]: 'unsupported',
  [TopicFailureCode.RESOURCE_EXHAUSTED]: 'over-limit',
  [TopicFailureCode.SOURCE_FAILED]: 'source-failed',
  [TopicFailureCode.INTERNAL]: 'internal',
};

type Frame<E> =
  | {
      readonly _tag: 'Next';
      readonly event: E | null;
      readonly receipt: 'heartbeat' | 'data' | 'value';
      readonly synced: boolean;
    }
  | {readonly _tag: 'End'; readonly end: TopicEnd};

const invalid = <E>(detail: string): Frame<E> => ({
  _tag: 'End',
  end: {_tag: 'Invalid', detail},
});

function readFrame<S, E>(
  descriptor: KeyedTopicDescriptor<S, E>,
  {frame}: SubscribeResponse,
): Frame<E> {
  switch (frame.case) {
    case 'entry': {
      const parsed = descriptor.entry(frame.value);
      return 'invalid' in parsed
        ? invalid(parsed.invalid)
        : {_tag: 'Next', event: parsed.event, receipt: 'value', synced: false};
    }
    case 'removed':
      return {
        _tag: 'Next',
        event: descriptor.removed(frame.value),
        receipt: 'data',
        synced: false,
      };
    case 'synced':
      return {_tag: 'Next', event: descriptor.synced, receipt: 'data', synced: true};
    case 'heartbeat':
      return {_tag: 'Next', event: null, receipt: 'heartbeat', synced: false};
    case 'failed': {
      const refusal = REFUSAL[frame.value.code];
      return refusal === null
        ? invalid('a failure with no code')
        : {_tag: 'End', end: {_tag: 'Refused', refusal}};
    }
    case undefined:
      return invalid('a frame with no content');
    default:
      frame satisfies never;
      return invalid('a frame of an unknown kind');
  }
}

interface Receipts {
  readonly frame: number;
  readonly data: number;
  readonly value: number;
}

interface Course {
  readonly snapshot: boolean;
  readonly aged: boolean;
  readonly receipts: Receipts;
  readonly syncedAt: number | null;
}

interface Outcome {
  readonly end: TopicEnd;
  readonly syncedAt: number | null;
}

type Turn =
  | {readonly _tag: 'On'; readonly course: Course}
  | {readonly _tag: 'Over'; readonly outcome: Outcome};

const received = (
  receipts: Receipts,
  receipt: 'heartbeat' | 'data' | 'value',
  at: number,
): Receipts => ({
  frame: at,
  data: receipt === 'heartbeat' ? receipts.data : at,
  value: receipt === 'value' ? at : receipts.value,
});

type Lapse = 'lease' | 'snapshot' | 'value';

const nextLapse = (
  course: Course,
  policy: KeyedTopicPolicy,
): {readonly at: number; readonly lapse: Lapse} => {
  const lease = {at: course.receipts.frame + subscribeTiming.leaseMs, lapse: 'lease'} as const;
  if (course.snapshot) {
    const snapshot = course.receipts.data + policy.firstFrameMs;
    return snapshot < lease.at ? {at: snapshot, lapse: 'snapshot'} : lease;
  }
  if (course.aged || policy.staleAfterMs === 0) return lease;
  const value = course.receipts.value + policy.staleAfterMs;
  return value < lease.at ? {at: value, lapse: 'value'} : lease;
};

const failed = (ports: KeyedTopicParts, epoch: number, failure: AppError | Cause.Done) =>
  Effect.gen(function* () {
    if (Cause.isDone(failure)) return {_tag: 'Closed'} satisfies TopicEnd;
    const end = {_tag: 'Failed', fault: faultOf(failure)} satisfies TopicEnd;
    if (topicRetry(end) === 'lost') yield* ports.connection.suspect(epoch, failure);
    return end;
  });

const lapsed = <S, E>(
  descriptor: KeyedTopicDescriptor<S, E>,
  course: Course,
  lapse: Lapse,
): Effect.Effect<Turn> =>
  Effect.gen(function* () {
    if (lapse !== 'value') {
      return {_tag: 'Over', outcome: {end: {_tag: 'Silent'}, syncedAt: course.syncedAt}};
    }
    if (descriptor.aged !== null) yield* descriptor.apply(descriptor.aged);
    return {_tag: 'On', course: {...course, aged: true}};
  });

const heard = <S, E>(
  ports: KeyedTopicParts,
  descriptor: KeyedTopicDescriptor<S, E>,
  course: Course,
  response: SubscribeResponse,
): Effect.Effect<Turn> =>
  Effect.gen(function* () {
    yield* ports.connection.heard;
    const now = yield* Clock.currentTimeMillis;
    const frame = readFrame(descriptor, response);
    if (frame._tag === 'End') {
      return {_tag: 'Over', outcome: {end: frame.end, syncedAt: course.syncedAt}};
    }
    const receipts = received(course.receipts, frame.receipt, now);
    if (frame.event === null) return {_tag: 'On', course: {...course, receipts}};
    const state = yield* descriptor.apply(frame.event);
    const broken = descriptor.invalid(state);
    if (broken !== null) {
      return {_tag: 'Over', outcome: {end: broken, syncedAt: course.syncedAt}};
    }
    return {
      _tag: 'On',
      course: {
        snapshot: frame.synced ? false : course.snapshot,
        aged: frame.receipt === 'value' ? false : course.aged,
        receipts,
        syncedAt: course.syncedAt ?? (frame.synced ? now : null),
      },
    };
  });

const pass = <S, E>(
  ports: KeyedTopicParts,
  descriptor: KeyedTopicDescriptor<S, E>,
  epoch: number,
): Effect.Effect<Outcome> =>
  Effect.scoped(
    Effect.gen(function* () {
      yield* descriptor.apply(descriptor.open);
      const frames = yield* Stream.toQueue(ports.client.subscribe(descriptor.request), {
        capacity: AHEAD,
      });
      const openedAt = yield* Clock.currentTimeMillis;
      let course: Course = {
        snapshot: true,
        aged: false,
        receipts: {frame: openedAt, data: openedAt, value: openedAt},
        syncedAt: null,
      };
      while (true) {
        const {at, lapse} = nextLapse(course, descriptor.policy);
        const wait = Duration.millis(Math.max(0, at - (yield* Clock.currentTimeMillis)));
        const taken = yield* Queue.take(frames).pipe(Effect.timeoutOption(wait), Effect.result);
        if (taken._tag === 'Failure') {
          return {end: yield* failed(ports, epoch, taken.failure), syncedAt: course.syncedAt};
        }
        const turn = Option.isNone(taken.success)
          ? yield* lapsed(descriptor, course, lapse)
          : yield* heard(ports, descriptor, course, taken.success.value);
        if (turn._tag === 'Over') return turn.outcome;
        course = turn.course;
      }
    }),
  );

const follow = <S, E>(
  ports: KeyedTopicParts,
  descriptor: KeyedTopicDescriptor<S, E>,
): Effect.Effect<never> =>
  Effect.gen(function* () {
    let after = -1;
    let failures = 0;
    while (true) {
      const ready = yield* ports.connection.ready(after).pipe(Effect.result);
      if (ready._tag === 'Failure') {
        yield* ports.connection.leftTerminal;
        continue;
      }
      const epoch = ready.success;
      const outcome = yield* pass(ports, descriptor, epoch);
      yield* descriptor.apply(descriptor.ended(outcome.end));
      if (topicRetry(outcome.end) !== 'backoff') {
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

/** A mounted atom that follows `descriptor` until interruption, then applies its Stop event. */
export function makeKeyedTopicWatch<S, E>(
  parts: KeyedTopicParts,
  descriptor: KeyedTopicDescriptor<S, E>,
): Atom.Atom<AsyncResult.AsyncResult<never, never>> {
  return Atom.make(
    follow(parts, descriptor).pipe(
      Effect.provideContext(parts.ambient),
      Effect.ensuring(descriptor.apply(descriptor.stop)),
    ),
  );
}
