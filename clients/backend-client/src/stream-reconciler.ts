import type {EventBatchMessage, RunEvent} from './protocol.js';

/**
 * The two facts about a batch the reconciler cannot know by itself: how the
 * subscription that delivered it was dialed, and where the caller's fold
 * currently floors.
 */
export interface BatchContext {
  /**
   * Whether the subscription that delivered this batch resumed from the
   * caller's cursor, rather than bootstrapping a fresh tail. This is the flag
   * `PersistentEventStream` binds when it dials, so a batch delivered
   * synchronously inside `subscribe` is tagged correctly too.
   */
  readonly resumed: boolean;
  /**
   * The history floor of what the caller has folded: the sequence below which
   * nothing has been read. Authoritative only for a resumed batch that is not
   * a re-bootstrap, which is the one disposition that declares no new floor;
   * every other disposition derives the floor from the batch itself.
   */
  readonly historyFloor: number;
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

/**
 * The `query.events` range one backfill covers, and the guard the reconciler
 * needs back to judge the response. Opaque to the caller apart from the two
 * bounds, which go straight into the request.
 */
export interface BackfillRequest {
  /**
   * `after_sequence`: the floor this chunk lowers the history floor to once it
   * is folded.
   */
  readonly afterSequence: number;
  /**
   * `before_sequence`. Every folded event has `sequence > floor`, so the range
   * has to include the floor itself and stops one above it.
   */
  readonly beforeSequence: number;
  /**
   * The log generation this range is addressed in. Only `settleBackfill`
   * interprets it.
   */
  readonly generation: number;
}

/**
 * How to fold one backfill response.
 *
 * `prepend` carries the events to fold as history older than everything
 * already folded, with the spine already filtered out, and the floor to record
 * afterwards. `superseded` means a re-bootstrap landed while the request was
 * in flight: drop the response and leave the floor where it is.
 */
export type BackfillReconciliation =
  | {readonly kind: 'prepend'; readonly events: readonly RunEvent[]; readonly historyFloor: number}
  | {readonly kind: 'superseded'};

export interface StreamReconcilerOptions {
  /**
   * How many events one backfill asks for. Defaults to 1000, matching the
   * bootstrap tail, so one backfill is one round trip of the same shape as the
   * boot subscribe; a caller that dials a different tail should pass it here.
   */
  backfillChunk?: number;
}

const DEFAULT_BACKFILL_CHUNK = 1_000;

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
 * and the generation that tells an in-flight backfill its log is gone.
 *
 * It decides; it never folds. Every method returns a disposition the caller
 * applies to its own state, so the same decisions serve any frontend and the
 * module stays free of state and UI types. It holds no I/O: the caller issues
 * the backfill request and hands back the response.
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
   * fold has no `sequence <= folded` guard to catch them. The set is
   * O(rounds), and filtering every chunk through it is what keeps one
   * `round_finished` from becoming two.
   */
  readonly #foldedBelowFloor = new Set<number>();
  /** Lowest history floor seen so far; see `#lowerFloor`. */
  #historyFloor = Number.POSITIVE_INFINITY;
  /**
   * The floor the stream itself last declared, which is not the same as the
   * floor the caller holds: backfill lowers the latter and the stream never
   * sees it. A later batch declaring more than this is a re-bootstrap within
   * one store, which is what the server does when a burst outruns the tail
   * bound; see `#rebootstrap`. Null until the first batch, whose floor is the
   * bootstrap's own and therefore raises nothing.
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
   * Bumped by every re-bootstrap, so an await that spans one can tell. A
   * backfill response is addressed in the sequence numbering of the log that
   * was streaming when the request left; once a batch swaps the store or
   * raises the floor, that numbering no longer describes the folded state, and
   * folding the response would splice a superseded log's events under the live
   * one.
   */
  #rebootstrapGeneration = 0;

  constructor(options: StreamReconcilerOptions = {}) {
    this.#backfillChunk = options.backfillChunk ?? DEFAULT_BACKFILL_CHUNK;
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
   * A resumed batch declares no new floor, so it keeps the caller's floor and
   * scrollback survives the reconnect. A fresh batch re-declares its
   * subscription's bootstrap floor on every delivery, including live ones, so
   * the floor is taken as a lower bound rather than literally. Either kind
   * re-bootstraps when the store it names is not the one the fold belongs to.
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
      return {kind: 'extend', historyFloor: context.historyFloor};
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
   * The range of the chunk of history just older than `historyFloor`, or null
   * when history is already complete.
   *
   * The returned request carries the generation of the log it is addressed in,
   * so obtaining a range and guarding its response are the same act: hand the
   * request back to `settleBackfill` once the server answers.
   *
   * One range at a time. The floor only moves when a response is folded, so
   * two requests taken against the same floor cover the same range and settle
   * to the same events twice. Single-flighting them is the caller's job: the
   * caller owns the request in flight, which this has no handle on.
   */
  beginBackfill(historyFloor: number): BackfillRequest | null {
    if (historyFloor === 0) return null;
    return {
      afterSequence: Math.max(0, historyFloor - this.#backfillChunk),
      beforeSequence: historyFloor + 1,
      generation: this.#rebootstrapGeneration,
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
   * `events` takes the response's field as it arrives, absent when the server
   * returned none.
   */
  settleBackfill(
    request: BackfillRequest,
    events: readonly RunEvent[] | undefined,
  ): BackfillReconciliation {
    if (request.generation !== this.#rebootstrapGeneration) return {kind: 'superseded'};
    const fresh = (events ?? []).filter(
      event => event.sequence === undefined || !this.#foldedBelowFloor.has(event.sequence),
    );
    return {kind: 'prepend', events: fresh, historyFloor: this.#lowerFloor(request.afterSequence)};
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
    this.#historyFloor = Math.min(this.#historyFloor, floor);
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
