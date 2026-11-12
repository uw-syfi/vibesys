/**
 * The loop every subscription runs: wait for a Ready connection, open the stream, fold each frame
 * into the host model, and say how it is going. It never retries on its own clock. A link-level
 * failure goes to `HostConnectionApi.suspect` and the loop waits for the next Ready epoch, so the
 * connection is the only place that counts failures and waits. What the loop decides itself is
 * which cursor to resume from and when to stop and show a failure.
 */
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {FeedStatus, HostModel} from '../state/index.js';
import type {HostConnectionApi} from './connection.js';
import {type AppError, isAheadOfLog, isRetryable, Unavailable} from './errors.js';
import {faultOf} from './fault.js';
import {MAX_QUIET_RESTARTS, STABLE_PASS_FRAMES} from './policy.js';

/** What differs between the run stream and an agent's stream. */
export interface FeedSpec<F> {
  /** The stream to open. `firstOpen` is true only for the first open since the feed was mounted. */
  readonly open: (
    model: HostModel,
    firstOpen: boolean,
    replacing: boolean,
  ) => Stream.Stream<F, AppError>;
  /** Reset stream-local ordering before opening, retaining displayed facts. */
  readonly begin?: (model: HostModel) => HostModel;
  /** Fold one frame. `replacing` is true through an explicitly requested replacement snapshot. */
  readonly apply: (model: HostModel, frame: F, replacing: boolean) => HostModel;
  /** The model skipped events and must read again from a snapshot. */
  readonly gapped: (model: HostModel) => boolean;
  /**
   * The model once the host said its log ends before this stream's cursor (`isAheadOfLog`): the
   * cursor is dropped, so the next open reads from the start. A stream that resumes from no cursor
   * has none, and that answer then fails it like any other refusal.
   */
  readonly rewind?: (model: HostModel) => HostModel;
}

/** What a feed reads and writes. */
export interface FeedPorts {
  readonly connection: HostConnectionApi;
  readonly model: SubscriptionRef.SubscriptionRef<HostModel>;
  readonly setStatus: (status: FeedStatus) => Effect.Effect<void>;
  /** Runs `announce`, then suspends until the user asks to retry (see `RetryGate.wait`). */
  readonly awaitRetry: (announce: Effect.Effect<void>) => Effect.Effect<void>;
}

/**
 * An optional user-requested replacement read. Its state belongs to the view that offers it; the
 * feed owns interrupting its current call and applying the replacement before reporting success.
 */
export interface FeedRefresh<F> {
  /** One wake-up per coalesced request; `pending` keeps the intent across an unmount. */
  readonly requests: Queue.Dequeue<true>;
  readonly pending: Effect.Effect<boolean>;
  /** Wake a failed feed or terminal connection once the feed has accepted the request. */
  readonly activate: Effect.Effect<void>;
  /** The frame that commits the replacement requested from `FeedSpec.open`. */
  readonly isReplacement: (frame: F) => boolean;
  readonly succeeded: (frame: F) => Effect.Effect<void>;
  readonly failed: Effect.Effect<void>;
}

type Why = Extract<FeedStatus, {readonly _tag: 'Restarting'}>['why'];

/** How one open stream ended. */
type Ended =
  /**
   * The stream, not the link, ended: a gap, a reader that fell behind, a cursor past the end of the
   * host's log, or a host that closed this stream cleanly. Open it again; `progressed` says the pass
   * delivered `STABLE_PASS_FRAMES` frames in order, so it was healthy and the restarts before it no
   * longer count.
   */
  | {readonly _tag: 'Restart'; readonly why: Why; readonly progressed: boolean}
  /** The link failed: hand it to the connection. */
  | {readonly _tag: 'Lost'; readonly error: AppError}
  /** This subscription cannot work (not found, invalid), whatever the link does. */
  | {readonly _tag: 'Failed'; readonly error: AppError};

interface PassResult {
  readonly ended: Ended;
  readonly replaced: boolean;
}

/**
 * Whether the failure says something about the link. An `Unimplemented` method or a missing
 * entity is about this subscription alone, so it fails this feed and leaves the link and the other
 * feeds running.
 */
const linkLevel = (error: AppError): boolean => isRetryable(error) || error._tag === 'NoBridge';

const classify = (error: AppError, progressed: boolean): Ended => {
  if (error._tag === 'FellBehind') return {_tag: 'Restart', why: 'fell-behind', progressed};
  return linkLevel(error) ? {_tag: 'Lost', error} : {_tag: 'Failed', error};
};

/** Open the stream once and fold it until it ends, a gap appears, or it fails. */
const pass = <F>(
  ports: FeedPorts,
  spec: FeedSpec<F>,
  firstOpen: boolean,
  replacing: boolean,
  refresh: FeedRefresh<F> | undefined,
) =>
  Effect.gen(function* () {
    let inOrder = 0;
    let live = false;
    let awaitingReplacement = replacing;
    let replaced = false;
    const opened = yield* spec.begin === undefined
      ? SubscriptionRef.get(ports.model)
      : SubscriptionRef.updateAndGet(ports.model, spec.begin);
    const result = yield* spec.open(opened, firstOpen, replacing).pipe(
      Stream.mapEffect(frame =>
        Effect.gen(function* () {
          // A frame is the host speaking: the host's freshness is as of now, whatever it says. A
          // replacement is folded before it is reported, so the old model stays whole until this
          // one atomic update swaps it out.
          yield* ports.connection.heard;
          const model = yield* SubscriptionRef.updateAndGet(ports.model, m =>
            spec.apply(m, frame, awaitingReplacement),
          );
          const gapped = spec.gapped(model);
          if (!gapped) inOrder += 1;
          if (!gapped && !live) {
            live = true;
            yield* ports.setStatus({_tag: 'Live'});
          }
          if (!gapped && awaitingReplacement && refresh?.isReplacement(frame)) {
            awaitingReplacement = false;
            replaced = true;
            yield* refresh.succeeded(frame);
          }
          return !gapped;
        }),
      ),
      Stream.takeWhile(keepGoing => keepGoing),
      Stream.runDrain,
      Effect.result,
    );
    const progressed = inOrder >= STABLE_PASS_FRAMES;
    if (result._tag === 'Failure') {
      const {failure} = result;
      if (isAheadOfLog(failure) && spec.rewind !== undefined) {
        // The host no longer has what the cursor points past: reopening from it is refused again.
        yield* SubscriptionRef.update(ports.model, spec.rewind);
        return {
          ended: {_tag: 'Restart', why: 'ahead-of-log', progressed},
          replaced,
        } satisfies PassResult;
      }
      return {ended: classify(failure, progressed), replaced} satisfies PassResult;
    }
    const closed = yield* SubscriptionRef.get(ports.model);
    // A gap ended the stream on purpose; otherwise the host closed this stream.
    return {
      ended: {
        _tag: 'Restart',
        why: spec.gapped(closed) ? 'gap' : 'reconnect',
        progressed,
      },
      replaced,
    } satisfies PassResult;
  });

interface Next {
  readonly quiet: number;
  readonly why: Why | null;
  /** The connection epoch the next `ready` must exceed. */
  readonly after: number;
}

/** What follows `ended`, which happened on `epoch` after `quiet` restarts that were not healthy. */
const afterPass = (ports: FeedPorts, epoch: number, quiet: number, ended: Ended) =>
  Effect.gen(function* () {
    const stay = epoch - 1;
    switch (ended._tag) {
      case 'Lost':
        yield* ports.connection.suspect(epoch, ended.error);
        return {quiet: 0, why: 'reconnect', after: epoch} satisfies Next;
      case 'Failed':
        yield* ports.awaitRetry(ports.setStatus({_tag: 'Failed', fault: faultOf(ended.error)}));
        return {quiet: 0, why: null, after: stay} satisfies Next;
      case 'Restart': {
        const quietNow = ended.progressed ? 0 : quiet + 1;
        if (quietNow <= MAX_QUIET_RESTARTS) {
          return {quiet: quietNow, why: ended.why, after: stay} satisfies Next;
        }
        // Reopening keeps ending before it is healthy: hand the connection the failure, whose
        // backoff then spaces the next open.
        const stuck = new Unavailable({detail: 'the stream keeps ending before it delivers'});
        yield* ports.connection.suspect(epoch, stuck);
        return {quiet: 0, why: 'reconnect', after: epoch} satisfies Next;
      }
    }
  });

type RefreshRace<A> = {readonly _tag: 'Completed'; readonly value: A} | {readonly _tag: 'Refresh'};

/** Run `effect` until it completes or a refresh asks the owner to interrupt it. */
const raceRefresh = <A, E, R, F>(
  effect: Effect.Effect<A, E, R>,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<RefreshRace<A>, E, R> => {
  const completed = Effect.map(effect, (value): RefreshRace<A> => ({_tag: 'Completed', value}));
  return refresh === undefined
    ? completed
    : Effect.raceFirst(
        completed,
        Effect.as(Queue.take(refresh.requests), {_tag: 'Refresh'} as const),
      );
};

/** Accept one queued request and wake the resources the feed may be waiting behind. */
const acceptRefresh = <F>(refresh: FeedRefresh<F>): Effect.Effect<void> => refresh.activate;

/** Whether a request was queued before this pass of the loop began. */
const queuedRefresh = <F>(refresh: FeedRefresh<F>): Effect.Effect<boolean> =>
  Effect.map(Queue.poll(refresh.requests), Option.isSome);

interface FeedLoopState {
  readonly next: Next;
  readonly firstOpen: boolean;
  readonly replacing: boolean;
}

type ConnectionStep =
  | {readonly _tag: 'Ready'; readonly epoch: number; readonly state: FeedLoopState}
  | {readonly _tag: 'Continue'; readonly state: FeedLoopState};

const INITIAL_LOOP: FeedLoopState = {
  next: {quiet: 0, why: null, after: -1},
  firstOpen: true,
  replacing: false,
};

/** Accept a request won by `raceRefresh`; that result proves a controller exists. */
const acceptRacedRefresh = <F>(
  state: FeedLoopState,
  refresh: FeedRefresh<F> | undefined,
  next: Next = state.next,
): Effect.Effect<FeedLoopState> =>
  refresh === undefined
    ? Effect.die(new Error('a feed refresh won a race without a refresh controller'))
    : Effect.as(acceptRefresh(refresh), {
        ...state,
        next: {...next, quiet: 0},
        replacing: true,
      });

/** Recover durable refresh intent when a view remounts, consuming its queued wake-up as well. */
const prepareRefresh = <F>(
  state: FeedLoopState,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<FeedLoopState> => {
  if (refresh === undefined || state.replacing) return Effect.succeed(state);
  return Effect.gen(function* () {
    // Poll first even when the durable state says pending, so the wake-up cannot interrupt the very
    // replacement pass it requested.
    const queued = yield* queuedRefresh(refresh);
    return queued || (yield* refresh.pending) ? yield* acceptRacedRefresh(state, refresh) : state;
  });
};

/** Finish an explicit refresh unsuccessfully, without changing ordinary feed failures. */
const failRefresh = <F>(
  state: FeedLoopState,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<FeedLoopState> =>
  !state.replacing || refresh === undefined
    ? Effect.succeed(state)
    : Effect.as(refresh.failed, {...state, replacing: false});

/** Wait for a usable connection, while allowing Refresh to wake a wait or terminal connection. */
const awaitConnection = <F>(
  ports: FeedPorts,
  state: FeedLoopState,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<ConnectionStep> =>
  Effect.gen(function* () {
    const waited = yield* raceRefresh(
      ports.connection.ready(state.next.after).pipe(Effect.result),
      refresh,
    );
    if (waited._tag === 'Refresh') {
      return {_tag: 'Continue', state: yield* acceptRacedRefresh(state, refresh)};
    }
    const ready = waited.value;
    if (ready._tag === 'Success') return {_tag: 'Ready', epoch: ready.success, state};

    const failed = yield* failRefresh(state, refresh);
    // Nothing can open until the user retries the connection. Refresh is itself such a retry.
    const left = yield* raceRefresh(ports.connection.leftTerminal, refresh);
    return left._tag === 'Completed'
      ? {_tag: 'Continue', state: failed}
      : {_tag: 'Continue', state: yield* acceptRacedRefresh(failed, refresh)};
  });

/** Continue after one stream pass ended, preserving the old RetryGate contract on failure. */
const afterCompletedPass = <F>(
  ports: FeedPorts,
  state: FeedLoopState,
  epoch: number,
  result: PassResult,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<FeedLoopState> =>
  Effect.gen(function* () {
    let current: FeedLoopState = {
      ...state,
      firstOpen: false,
      replacing: result.replaced ? false : state.replacing,
    };
    if (current.replacing && result.ended._tag === 'Failed') {
      current = yield* failRefresh(current, refresh);
    }
    if (result.ended._tag !== 'Failed') {
      const next = yield* afterPass(ports, epoch, current.next.quiet, result.ended);
      return {...current, next};
    }

    // A failed feed waits for its existing RetryGate, but a new refresh may also wake it and ask
    // for a replacement snapshot. Feed users without a refresh keep the original behavior exactly.
    const after = yield* raceRefresh(
      afterPass(ports, epoch, current.next.quiet, result.ended),
      refresh,
    );
    return after._tag === 'Completed'
      ? {...current, next: after.value}
      : yield* acceptRacedRefresh(current, refresh, {
          quiet: 0,
          why: null,
          after: epoch - 1,
        });
  });

/** Open and follow one pass, interrupting only this stream when Refresh wins the race. */
const followPass = <F>(
  ports: FeedPorts,
  spec: FeedSpec<F>,
  state: FeedLoopState,
  epoch: number,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<FeedLoopState> =>
  Effect.gen(function* () {
    const cycle = yield* raceRefresh(
      pass(ports, spec, state.firstOpen, state.replacing, refresh),
      refresh,
    );
    const opened = {...state, firstOpen: false};
    return cycle._tag === 'Completed'
      ? yield* afterCompletedPass(ports, opened, epoch, cycle.value, refresh)
      : yield* acceptRacedRefresh(opened, refresh, {
          quiet: 0,
          why: null,
          after: epoch - 1,
        });
  });

/** One control step: recover a pending request, obtain a connection, then follow one stream. */
const feedStep = <F>(
  ports: FeedPorts,
  spec: FeedSpec<F>,
  state: FeedLoopState,
  refresh: FeedRefresh<F> | undefined,
): Effect.Effect<FeedLoopState> =>
  Effect.gen(function* () {
    const prepared = yield* prepareRefresh(state, refresh);
    yield* ports.setStatus(
      prepared.next.why === null ? {_tag: 'Opening'} : {_tag: 'Restarting', why: prepared.next.why},
    );
    const connection = yield* awaitConnection(ports, prepared, refresh);
    return connection._tag === 'Continue'
      ? connection.state
      : yield* followPass(ports, spec, connection.state, connection.epoch, refresh);
  });

/** Runs for as long as the feed is mounted: the caller interrupts it to unmount. */
export const runFeed = <F>(
  ports: FeedPorts,
  spec: FeedSpec<F>,
  refresh?: FeedRefresh<F>,
): Effect.Effect<never> =>
  Effect.gen(function* () {
    let state = INITIAL_LOOP;
    while (true) state = yield* feedStep(ports, spec, state, refresh);
  });
