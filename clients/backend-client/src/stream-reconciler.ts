import {BackendClientError} from './errors.js';
import type {EventBatchMessage, RunEvent} from './protocol.js';

/**
 * The one fact about a batch the reconciler cannot know by itself: how the
 * subscription that delivered it was dialed.
 */
export interface BatchContext {
  /**
   * Whether the subscription that delivered this batch resumed from the
   * caller's cursor, rather than bootstrapping a fresh tail. This is the flag
   * `PersistentEventStream` binds when it dials, so a batch delivered
   * synchronously inside `subscribe` is tagged correctly too.
   */
  readonly resumed: boolean;
}

/**
 * How to fold one `event_batch` against what is already folded.
 *
 * `extend` keeps the folded state and adds this batch to it. `rebootstrap`
 * says the folded state describes a log this batch supersedes: discard it and
 * fold this batch as the new bootstrap. Either way `historyFloor` is the value
 * to record as the folded state's `history_after_sequence`.
 */
export type BatchReconciliation =
  | {readonly kind: 'extend'; readonly historyFloor: number}
  | {readonly kind: 'rebootstrap'; readonly historyFloor: number};

declare const backfillStampBrand: unique symbol;

/**
 * Identifies one issued range. Opaque: only `settleBackfill` and
 * `abandonBackfill` read it, and it cannot be constructed outside this module,
 * so a forged or rehydrated request cannot defeat the supersession guard.
 */
export type BackfillStamp = number & {readonly [backfillStampBrand]: never};

/** The `query.events` range one backfill covers. Both bounds are exclusive. */
export interface BackfillRequest {
  /** `after_sequence`. The server answers `after_sequence < sequence`. */
  readonly afterSequence: number;
  /**
   * `before_sequence`. Every folded event has `sequence > floor`, so the range
   * has to include the floor itself and stops one above it.
   */
  readonly beforeSequence: number;
  readonly stamp: BackfillStamp;
}

/**
 * Whether there is a range to ask for.
 *
 * `fetch` carries it. `complete` means the folded log already reaches back to
 * the start, so there is nothing older. `in-flight` means a range is already
 * outstanding: the floor moves only when a response is folded, so a second
 * range taken now would duplicate the first. Callers that share one answer
 * between concurrent readers do that above this, which is where the request
 * itself lives.
 */
export type BackfillPlan =
  | {readonly kind: 'fetch'; readonly request: BackfillRequest}
  | {readonly kind: 'complete'}
  | {readonly kind: 'in-flight'};

/**
 * How to fold one backfill response.
 *
 * `prepend` carries the events to fold as history older than everything
 * already folded, with the spine already filtered out, and the floor to record
 * afterwards. `superseded` means the response no longer describes the folded
 * log: a re-bootstrap landed while the request was in flight, or the request
 * is not the outstanding one. Drop it and leave the floor where it is.
 */
export type BackfillReconciliation =
  | {readonly kind: 'prepend'; readonly events: readonly RunEvent[]; readonly historyFloor: number}
  | {readonly kind: 'superseded'};

export interface StreamReconcilerOptions {
  /**
   * How many events one backfill asks for. Required rather than defaulted: the
   * value belongs with the subscription's bootstrap tail, so that one backfill
   * is one round trip of the same shape as the boot subscribe, and a default
   * here would let the two drift apart silently.
   */
  readonly backfillChunk: number;
}

/** The range currently outstanding, and what it was addressed in. */
interface OutstandingRange {
  readonly stamp: number;
  readonly generation: number;
  readonly afterSequence: number;
}

function positiveInteger(name: string, value: number): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new RangeError(`${name} must be a positive integer, received ${value}`);
  }
  return value;
}

/**
 * The floor the batch declares, rejected at the boundary when the wire carries
 * something that cannot be a sequence. `history_after_sequence` is `ge=0` in
 * the protocol models, so a conforming server never trips this; validating
 * here keeps `historyFloor >= 0` an invariant of this module rather than a
 * precondition every caller has to restate, and stops a negative floor from
 * reaching `beginBackfill` as a range the server would reject.
 */
function declaredFloorOf(message: EventBatchMessage): number {
  const declared = message.history_after_sequence ?? 0;
  if (!Number.isSafeInteger(declared) || declared < 0) {
    throw new BackendClientError(
      'parse',
      `event_batch.history_after_sequence must be a non-negative integer, received ${declared}`,
    );
  }
  return declared;
}

/**
 * Keeps a subscription's folded sequence space correct across resumes,
 * re-bootstraps, and history backfills.
 *
 * A subscription is not a stable numbering. The run's durable event log is
 * attached after the client subscribes, so a stream that bootstrapped against
 * the server's own short log is re-bootstrapped against the run log, and a
 * burst that outruns the tail bound re-bootstraps within one store. Meanwhile
 * the reader scrolls back and backfills history below the floor. This owns the
 * arithmetic that keeps those three from corrupting each other: store
 * identity, the history floor, the run-level spine replayed below the floor,
 * and the range outstanding against a log that may be gone by the time it
 * answers.
 *
 * It decides; it never folds. Every method returns a disposition the caller
 * applies to its own state, so the same decisions serve any frontend and the
 * module stays free of state and UI types. It holds no I/O: the caller issues
 * the request and hands back the answer, or says the request failed.
 *
 * The floor is this module's own, not a value the caller passes back in, so a
 * caller that mislays a disposition cannot talk it into a floor below the
 * range it just fetched.
 */
export class StreamReconciler {
  readonly #backfillChunk: number;
  /**
   * Sequences already folded from below the history floor.
   *
   * A tail subscription's batch is not only the tail: the server also replays
   * the run-level spine from before the floor (`run_started`,
   * `round_finished`, `chat_thread_created`, the terminal events, ...) so the
   * ordinary reducer can derive what a suffix cannot carry. A backfill chunk
   * covering that range therefore re-delivers those same events, and a prefix
   * fold has no `sequence <= folded` guard to catch them. The set stays
   * O(rounds) because only events at or below the declaring batch's own floor
   * go in, and filtering every chunk through it is what keeps one
   * `round_finished` from becoming two.
   */
  readonly #foldedBelowFloor = new Set<number>();
  /**
   * Lowest history floor reached so far, or null before any batch has declared
   * one. Null rather than zero: a stream whose first delivery is a resume (not
   * reachable through `PersistentEventStream`, which resumes only from a
   * cursor a bootstrap established) has declared no floor at all, and must not
   * be treated as one that declared the start of the log.
   */
  #historyFloor: number | null = null;
  /**
   * The floor the stream itself last declared, which is not the same as the
   * floor reached: backfill lowers that and the stream never sees it. A later
   * batch declaring more than this is a re-bootstrap within one store, which
   * is what the server does when a burst outruns the tail bound. Null until
   * the first fresh batch, whose floor is the bootstrap's own and therefore
   * raises nothing. A resume never writes it, because a resume re-declares the
   * floor its subscription booted with rather than a new one.
   */
  #declaredFloor: number | null = null;
  /**
   * The event store the folded sequences belong to, as the stream last named
   * it. Sequences only mean anything within one store, and a run swaps in its
   * durable log after the client subscribes, so a batch that names a different
   * store supersedes the fold however the two logs compare in length. Null
   * until the first batch, and empty against a server that does not report
   * identity, which leaves `#declaredFloor` as the only signal.
   */
  #storeId: string | null = null;
  /**
   * Bumped by every re-bootstrap, so a range that spans one can tell. A
   * backfill response is addressed in the sequence numbering of the log that
   * was streaming when the request left; once a batch swaps the store or
   * raises the floor, that numbering no longer describes the folded state, and
   * folding the response would splice a superseded log's events under the live
   * one.
   */
  #rebootstrapGeneration = 0;
  /** Distinguishes every range ever issued, so a late duplicate cannot pass as the live one. */
  #issued = 0;
  #outstanding: OutstandingRange | null = null;

  constructor(options: StreamReconcilerOptions) {
    this.#backfillChunk = positiveInteger('backfillChunk', options.backfillChunk);
  }

  /**
   * The store the folded sequences belong to, for a resume to name so the
   * server can tell whether the cursor still numbers the live store. Empty
   * until a batch has named one, in which case the resume is a plain cursor
   * resume.
   */
  storeId(): string {
    return this.#storeId ?? '';
  }

  /**
   * Decides how one `event_batch` folds, and records what the batch teaches
   * about the stream.
   *
   * A resumed batch declares no new floor, so it keeps the floor already
   * reached and scrollback survives the reconnect. A fresh batch re-declares
   * its subscription's bootstrap floor on every delivery, including live ones,
   * so that floor is taken as a lower bound rather than literally. Either kind
   * re-bootstraps when the store it names is not the one the fold belongs to.
   *
   * Throws when `history_after_sequence` cannot be a sequence; see
   * `declaredFloorOf`.
   */
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation {
    const declared = declaredFloorOf(message);
    const store = message.store_id ?? '';
    if (context.resumed) {
      // A changed store invalidates the cursor and its folded state. The
      // resumed batch is therefore a fresh bootstrap, including spine tracking.
      //
      // An empty name on either side suppresses the comparison: a server that
      // does not report identity leaves nothing to disagree about. The fresh
      // path below does not suppress it, so the same pair of batches decides
      // differently depending on how the subscription was dialed.
      const knownStoreChanged =
        this.#storeId !== null && this.#storeId !== '' && store !== '' && store !== this.#storeId;
      if (knownStoreChanged) return this.#rebootstrap(store, declared, message.events);
      if (store !== '') this.#storeId = store;
      return {kind: 'extend', historyFloor: this.#historyFloor ?? 0};
    }
    const rebootstrap =
      (this.#storeId !== null && store !== this.#storeId) ||
      (this.#declaredFloor !== null && declared > this.#declaredFloor);
    if (rebootstrap) return this.#rebootstrap(store, declared, message.events);
    this.#storeId = store;
    this.#declaredFloor = declared;
    const historyFloor = this.#lowerFloor(declared);
    this.#recordSpine(message.events, declared);
    return {kind: 'extend', historyFloor};
  }

  /**
   * The chunk of history just older than the floor reached so far.
   *
   * Taking a range and guarding its answer are the same act: the returned
   * request is the only thing `settleBackfill` and `abandonBackfill` accept, so
   * a caller cannot fold an answer without the stamp that judges it. Exactly
   * one range is outstanding at a time, and it stays outstanding until the
   * caller settles or abandons it, so release it on every path.
   */
  beginBackfill(): BackfillPlan {
    const floor = this.#historyFloor ?? 0;
    // Complete before outstanding, the order `loadOlderHistory` checks them:
    // a log with no older history has nothing to wait for either. Equality is
    // the whole test because the floor is non-negative by construction:
    // `declaredFloorOf` rejects anything else at the boundary, and the only
    // other source is the clamped `afterSequence` below.
    if (floor === 0) return {kind: 'complete'};
    if (this.#outstanding !== null) return {kind: 'in-flight'};
    this.#issued += 1;
    const afterSequence = Math.max(0, floor - this.#backfillChunk);
    this.#outstanding = {
      stamp: this.#issued,
      generation: this.#rebootstrapGeneration,
      afterSequence,
    };
    return {
      kind: 'fetch',
      request: {
        afterSequence,
        beforeSequence: floor + 1,
        stamp: this.#issued as BackfillStamp,
      },
    };
  }

  /**
   * Judges a backfill response against the log it was addressed in, and
   * filters the spine the tail already delivered out of what is left.
   *
   * A re-bootstrap while the request was in flight replaced that log; the
   * response describes the superseded one and must not fold under the fresh
   * one, nor drag its floor down. The next ask backfills against the new log's
   * own numbering. Spine events replayed with the tail fall inside this range,
   * and folding them a second time would duplicate their transcript entries.
   *
   * `events` is what the server returned for the range, which is empty when
   * the range holds nothing. A request that failed is not that: report it
   * through `abandonBackfill`, which leaves the floor where it was so the same
   * range is asked for again.
   */
  settleBackfill(request: BackfillRequest, events: readonly RunEvent[]): BackfillReconciliation {
    const settled = this.#release(request);
    if (settled === null || settled.generation !== this.#rebootstrapGeneration) {
      return {kind: 'superseded'};
    }
    const fresh = events.filter(candidate => {
      const {sequence} = candidate;
      // An unsequenced event cannot be recognized in a later chunk, so it is
      // never recorded and never filtered. The branch is what narrows
      // `sequence` to the set's element type, so it cannot rot into a no-op.
      if (sequence === undefined) return true;
      return !this.#foldedBelowFloor.has(sequence);
    });
    // The stored bound, not the request's, so a tampered copy cannot move the
    // floor somewhere the fetched range does not justify.
    return {kind: 'prepend', events: fresh, historyFloor: this.#lowerFloor(settled.afterSequence)};
  }

  /**
   * Gives up the outstanding range without folding anything, for a request
   * that failed or was cancelled. The floor stays where it was, so the same
   * range is asked for again the next time the reader wants it. A no-op for a
   * range that is not the outstanding one.
   */
  abandonBackfill(request: BackfillRequest): void {
    this.#release(request);
  }

  /** Takes the outstanding range if `request` is it, else reports nothing to take. */
  #release(request: BackfillRequest): OutstandingRange | null {
    const outstanding = this.#outstanding;
    if (outstanding === null || outstanding.stamp !== request.stamp) return null;
    this.#outstanding = null;
    return outstanding;
  }

  /**
   * The floor only ever descends.
   *
   * A subscription reports the floor it bootstrapped with on every batch it
   * sends, including live ones. Once a backfill has lowered the floor, taking
   * a later batch's value literally would raise it again and send the client
   * back for history it already holds.
   */
  #lowerFloor(floor: number): number {
    this.#historyFloor = this.#historyFloor === null ? floor : Math.min(this.#historyFloor, floor);
    return this.#historyFloor;
  }

  /**
   * Takes a re-bootstrapped stream's floor literally, up or down.
   *
   * The run's durable event log is attached after the client subscribes, so a
   * subscription that bootstrapped against the server's own short log is
   * re-bootstrapped against the run log. Everything below the new floor is
   * unread history, whatever the caller held before, and the spine set
   * described a log this one replaces. Descending is not the backfill's
   * descent either: a run log shorter than the tail is replayed whole and
   * declares floor 0, which is the truth about the log now being streamed.
   *
   * An outstanding range is left outstanding: it is still a request in flight
   * that the caller owes an answer for, and the generation recorded with it
   * now differs, so settling it reports `superseded`.
   */
  #rebootstrap(store: string, declared: number, events: readonly RunEvent[]): BatchReconciliation {
    this.#storeId = store;
    this.#declaredFloor = declared;
    this.#rebootstrapGeneration += 1;
    this.#historyFloor = declared;
    this.#foldedBelowFloor.clear();
    this.#recordSpine(events, declared);
    return {kind: 'rebootstrap', historyFloor: declared};
  }

  /** Remembers the events a batch delivered from below its own history floor. */
  #recordSpine(events: readonly RunEvent[], historyAfterSequence: number): void {
    if (historyAfterSequence === 0) return;
    for (const {sequence} of events) {
      // An unsequenced event cannot be recognized in a later chunk anyway.
      if (sequence !== undefined && sequence <= historyAfterSequence) {
        this.#foldedBelowFloor.add(sequence);
      }
    }
  }
}
