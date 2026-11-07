/**
 * The loop every subscription runs: wait for a Ready connection, open the stream, fold each frame
 * into the host model, and say how it is going. It never retries on its own clock. A link-level
 * failure goes to `HostConnectionApi.suspect` and the loop waits for the next Ready epoch, so the
 * connection is the only place that counts failures and waits. What the loop decides itself is
 * which cursor to resume from and when to stop and show a failure.
 */
import * as Effect from 'effect/Effect';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {FeedStatus, HostModel} from '../state/index.js';
import type {HostConnectionApi} from './connection.js';
import {type AppError, isRetryable, Unavailable} from './errors.js';
import {faultOf} from './fault.js';
import {MAX_QUIET_RESTARTS, STABLE_PASS_FRAMES} from './policy.js';

/** What differs between the run stream and an agent's stream. */
export interface FeedSpec<F> {
  /** The stream to open. `firstOpen` is true only for the first open since the feed was mounted. */
  readonly open: (model: HostModel, firstOpen: boolean) => Stream.Stream<F, AppError>;
  /** Reset stream-local ordering before opening, retaining displayed facts. */
  readonly begin?: (model: HostModel) => HostModel;
  readonly apply: (model: HostModel, frame: F) => HostModel;
  /** The model skipped events and must read again from a snapshot. */
  readonly gapped: (model: HostModel) => boolean;
}

/** What a feed reads and writes. */
export interface FeedPorts {
  readonly connection: HostConnectionApi;
  readonly model: SubscriptionRef.SubscriptionRef<HostModel>;
  readonly setStatus: (status: FeedStatus) => Effect.Effect<void>;
  /** Runs `announce`, then suspends until the user asks to retry (see `RetryGate.wait`). */
  readonly awaitRetry: (announce: Effect.Effect<void>) => Effect.Effect<void>;
}

type Why = Extract<FeedStatus, {readonly _tag: 'Restarting'}>['why'];

/** How one open stream ended. */
type Ended =
  /**
   * The stream, not the link, ended: a gap, a reader that fell behind, or a host that closed this
   * stream cleanly. Open it again; `progressed` says the pass delivered `STABLE_PASS_FRAMES`
   * frames in order, so it was healthy and the restarts before it no longer count.
   */
  | {readonly _tag: 'Restart'; readonly why: Why; readonly progressed: boolean}
  /** The link failed: hand it to the connection. */
  | {readonly _tag: 'Lost'; readonly error: AppError}
  /** This subscription cannot work (not found, invalid), whatever the link does. */
  | {readonly _tag: 'Failed'; readonly error: AppError};

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
const pass = <F>(ports: FeedPorts, spec: FeedSpec<F>, firstOpen: boolean) =>
  Effect.gen(function* () {
    let inOrder = 0;
    let live = false;
    const opened = yield* spec.begin === undefined
      ? SubscriptionRef.get(ports.model)
      : SubscriptionRef.updateAndGet(ports.model, spec.begin);
    const result = yield* spec.open(opened, firstOpen).pipe(
      Stream.mapEffect(frame =>
        Effect.gen(function* () {
          // A frame is the host speaking: the host's freshness is as of now, whatever it says.
          yield* ports.connection.heard;
          const model = yield* SubscriptionRef.updateAndGet(ports.model, m => spec.apply(m, frame));
          const gapped = spec.gapped(model);
          if (!gapped) inOrder += 1;
          if (!gapped && !live) {
            live = true;
            yield* ports.setStatus({_tag: 'Live'});
          }
          return !gapped;
        }),
      ),
      Stream.takeWhile(keepGoing => keepGoing),
      Stream.runDrain,
      Effect.result,
    );
    const progressed = inOrder >= STABLE_PASS_FRAMES;
    if (result._tag === 'Failure') return classify(result.failure, progressed);
    const closed = yield* SubscriptionRef.get(ports.model);
    // A gap ended the stream on purpose; otherwise the host closed this stream.
    return {_tag: 'Restart', why: spec.gapped(closed) ? 'gap' : 'reconnect', progressed} as const;
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

/** Runs for as long as the feed is mounted: the caller interrupts it to unmount. */
export const runFeed = <F>(ports: FeedPorts, spec: FeedSpec<F>): Effect.Effect<never> =>
  Effect.gen(function* () {
    let next: Next = {quiet: 0, why: null, after: -1};
    let firstOpen = true;
    while (true) {
      yield* ports.setStatus(
        next.why === null ? {_tag: 'Opening'} : {_tag: 'Restarting', why: next.why},
      );
      const ready = yield* ports.connection.ready(next.after).pipe(Effect.result);
      if (ready._tag === 'Failure') {
        // Nothing can open until the user retries the connection.
        yield* ports.connection.leftTerminal;
        continue;
      }
      const epoch = ready.success;
      const ended = yield* pass(ports, spec, firstOpen);
      firstOpen = false;
      next = yield* afterPass(ports, epoch, next.quiet, ended);
    }
  });
