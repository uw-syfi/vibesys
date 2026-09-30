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

/** The `query.events` range one backfill covers. Both bounds are exclusive. */
export interface BackfillRequest {
  /** `after_sequence`. The server answers `after_sequence < sequence`. */
  readonly afterSequence: number;
  /**
   * `before_sequence`. Every folded event has `sequence > floor`, so the range
   * has to include the floor itself and stops one above it.
   */
  readonly beforeSequence: number;
}

/**
 * Issues one `query.events` and answers with what the range held, which is
 * empty when it held nothing.
 *
 * The reconciler computes the range and drives the call; the caller owns the
 * transport, the timeout, and whether a failure is worth reporting. A rejection
 * means the request did not complete, which is not the same as an empty range:
 * it leaves the floor where it was and propagates to whoever asked for the
 * backfill.
 */
export type BackfillFetch = (request: BackfillRequest) => Promise<readonly RunEvent[]>;

/**
 * How one backfill turned out.
 *
 * `prepend` carries the events to fold as history older than everything
 * already folded, with the spine already filtered out, and the floor to record
 * afterwards. `complete` means the folded log already reaches back to the start
 * of the log, so nothing was asked for. `superseded` means a re-bootstrap
 * landed while the request was in flight, so the answer describes a log the
 * fold no longer belongs to: drop it and leave the floor where it is.
 */
export type BackfillOutcome =
  | {readonly kind: 'prepend'; readonly events: readonly RunEvent[]; readonly historyFloor: number}
  | {readonly kind: 'complete'}
  | {readonly kind: 'superseded'};

const COMPLETE: BackfillOutcome = {kind: 'complete'};
const SUPERSEDED: BackfillOutcome = {kind: 'superseded'};

export interface StreamReconcilerOptions {
  /**
   * How many events one backfill asks for. Required rather than defaulted: the
   * value belongs with the subscription's bootstrap tail, so that one backfill
   * is one round trip of the same shape as the boot subscribe, and a default
   * here would let the two drift apart silently.
   */
  readonly backfillChunk: number;
}

/**
 * An out-of-range option is a bug in the code that wired the reconciler up,
 * not an outcome of talking to a server, so it is outside `BackendErrorKind`;
 * see the note on `BackendClientError`.
 */
function positiveInteger(name: string, value: number): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new RangeError(`${name} must be a positive integer, received ${value}`);
  }
  return value;
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
 * It decides; it never folds. Every method answers with a disposition the
 * caller applies to its own state, so the same decisions serve any frontend and
 * the module stays free of state and UI types. It holds no I/O either: the one
 * effect it needs, a `query.events` round trip, is injected per call, so a test
 * substitutes a Fake and neither transport is reachable from here.
 *
 * Both pieces of state a caller could corrupt are the module's own. The floor is
 * its field rather than a value threaded back in, so a caller that mislays a
 * disposition cannot talk it into a floor below the range it just fetched. The
 * outstanding range is a resource it owns end to end, released when the fetch
 * settles, so there is no obligation for a caller to forget.
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
   * raises nothing.
   *
   * A resume writes it only on the store-change branch, which is a full
   * bootstrap. That writes 0: a tail-less subscription reports
   * `history_after_sequence = 0` on the wire, whatever cursor it resumed from
   * (`reported_floor = 0 if request.tail is None`,
   * `src/server/transport/websocket.py:390`), because nothing was withheld. So
   * the next tail dial declares a floor above 0 and re-bootstraps again, even
   * though the store did not change. TUI-identical
   * (`session-controller.ts:1436`) and pinned by the resumed store-swap test.
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
  /**
   * The backfill in flight, or null when none is. Holding the promise rather
   * than the range is what lets a second reader join the round trip instead of
   * being turned away, and what ties the slot's lifetime to something that
   * always settles.
   */
  #outstanding: Promise<BackfillOutcome> | null = null;

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
   * A resumed batch declares no new floor (its subscription withheld nothing,
   * so the wire value is 0), and the floor already reached is kept, so
   * scrollback survives the reconnect. A fresh batch re-declares its
   * subscription's bootstrap floor on every delivery, including live ones, so
   * that floor is taken as a lower bound rather than literally. Either kind
   * re-bootstraps when the store it names is not the one the fold belongs to.
   *
   * Takes a message that came through `parseServerMessage`, which refuses a
   * `history_after_sequence` that cannot be a sequence. That is what makes
   * `historyFloor >= 0` an invariant here instead of a precondition every
   * caller restates, and it is checked once for both clients rather than split
   * across them.
   */
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation {
    const declared = message.history_after_sequence ?? 0;
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
   * Fetches the chunk of history just older than the floor reached so far, and
   * judges the answer against the log the range was addressed in.
   *
   * The reconciler drives the fetch rather than handing the range out, because
   * the range is a resource it owns: one is outstanding at a time, and the slot
   * has to be released however the request ends, a rejection included. Handing
   * the range out and asking for a report back makes that the caller's
   * obligation, and a caller that forgets it holds the slot forever, which
   * stops scrollback with nothing to show for it. A `finally` on the injected
   * fetch cannot be forgotten.
   *
   * Concurrent readers share one round trip. A reader holding the scroll
   * gesture at the top asks repeatedly and the floor moves only when an answer
   * is folded, so while a range is outstanding this returns that same promise
   * instead of asking for the same range twice. It is one answer, so a caller
   * with more than one reader must fold it once.
   *
   * A rejection propagates with the floor untouched, so the same range is asked
   * for again the next time the reader wants it. Whether a failure is worth
   * showing is the caller's decision, so it is not classified here.
   */
  backfill(fetch: BackfillFetch): Promise<BackfillOutcome> {
    const floor = this.#historyFloor ?? 0;
    // Complete before outstanding, the order `loadOlderHistory` checks them: a
    // log with no older history has nothing to wait for either. Equality is the
    // whole test because the floor is non-negative: `parseServerMessage`
    // refuses any other declared floor, and the only other source is the
    // clamped `afterSequence` below.
    if (floor === 0) return Promise.resolve(COMPLETE);
    const outstanding = this.#outstanding;
    if (outstanding !== null) return outstanding;
    const running = this.#fetchBelow(floor, fetch).finally(() => {
      this.#outstanding = null;
    });
    this.#outstanding = running;
    return running;
  }

  /**
   * One round trip, in the numbering of the log that was streaming when it
   * left.
   *
   * The generation is captured before the await. A re-bootstrap while the
   * request is in flight replaced that log; the answer describes the superseded
   * one and must not fold under the fresh one, nor drag its floor down. The
   * next ask backfills against the new log's own numbering.
   *
   * What it does not do is police the events against the range: `query.events`
   * bounds what the server returns, and the controller this is extracted from
   * checks neither bound, so checking here would be a behavior change in a
   * refactor. Only the floor is judged, against the range this method computed
   * rather than anything it was handed. Spine events replayed with the tail
   * fall inside the range, and folding them a second time would duplicate their
   * transcript entries, so they are filtered out.
   */
  async #fetchBelow(floor: number, fetch: BackfillFetch): Promise<BackfillOutcome> {
    const afterSequence = Math.max(0, floor - this.#backfillChunk);
    const generation = this.#rebootstrapGeneration;
    const events = await fetch({afterSequence, beforeSequence: floor + 1});
    if (generation !== this.#rebootstrapGeneration) return SUPERSEDED;
    const fresh = events.filter(candidate => {
      const {sequence} = candidate;
      // An unsequenced event cannot be recognized in a later chunk, so it is
      // never recorded and never filtered. The branch is what narrows
      // `sequence` to the set's element type, so it cannot rot into a no-op.
      if (sequence === undefined) return true;
      return !this.#foldedBelowFloor.has(sequence);
    });
    return {kind: 'prepend', events: fresh, historyFloor: this.#lowerFloor(afterSequence)};
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
   * An outstanding range is left outstanding. Nothing can cancel it: the
   * protocol carries no store or generation on `query.events`, the server keeps
   * no registry of outstanding requests, and a subscribed connection serves no
   * further requests at all, so the round trip really is still in flight and
   * discarding its answer is the only available answer. The generation captured
   * with it now differs, so that answer comes back `superseded`.
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
