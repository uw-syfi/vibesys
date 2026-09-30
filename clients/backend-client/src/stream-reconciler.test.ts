import {describe, expect, it} from 'bun:test';
import type {EventBatchMessage, RunEvent} from './protocol.js';
import {
  type BackfillFetch,
  type BackfillOutcome,
  type BackfillRequest,
  type BatchContext,
  type BatchReconciliation,
  StreamReconciler,
} from './stream-reconciler.js';

const SPINE_TYPE = 'round_finished';
const TAIL_TYPE = 'agent_output_chunk';

const FRESH: BatchContext = {resumed: false};
const RESUMED: BatchContext = {resumed: true};

function event(sequence: number | undefined, type: RunEvent['type'] = TAIL_TYPE): RunEvent {
  const base: RunEvent = {type, timestamp: '2026-01-01T00:00:00Z'};
  return sequence === undefined ? base : {...base, sequence};
}

interface BatchSpec {
  /** Omitted leaves the key off the payload, as a server that reports no identity does. */
  readonly storeId?: string;
  /** Omitted leaves the key off the payload. */
  readonly declaredFloor?: number;
  readonly sequences?: readonly (number | undefined)[];
}

function batch(spec: BatchSpec = {}): EventBatchMessage {
  const message: EventBatchMessage = {
    type: 'event_batch',
    events: (spec.sequences ?? []).map(sequence => event(sequence)),
  };
  if (spec.storeId !== undefined) message.store_id = spec.storeId;
  if (spec.declaredFloor !== undefined) message.history_after_sequence = spec.declaredFloor;
  return message;
}

function sequences(events: readonly RunEvent[]): readonly (number | undefined)[] {
  return events.map(item => item.sequence);
}

function prepended(outcome: BackfillOutcome): {
  readonly events: readonly RunEvent[];
  readonly historyFloor: number;
} {
  if (outcome.kind !== 'prepend') throw new Error(`expected a prepend, got ${outcome.kind}`);
  return outcome;
}

/** The error `promise` rejected with, so a test can assert on it. */
async function rejected(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('expected a rejection');
}

/** A promise a test settles on command, so a request stays in flight without a timer. */
interface Gate {
  readonly promise: Promise<readonly RunEvent[]>;
  readonly answer: (events: readonly RunEvent[]) => void;
  readonly fail: (error: Error) => void;
}

function gate(): Gate {
  let answer!: (events: readonly RunEvent[]) => void;
  let fail!: (error: Error) => void;
  const promise = new Promise<readonly RunEvent[]>((resolve, reject) => {
    answer = resolve;
    fail = reject;
  });
  return {promise, answer, fail};
}

/**
 * A fake `query.events`: records every range it was asked for and answers each
 * only when the test says so, which is what lets a test hold a request in
 * flight across a batch with no reliance on timing.
 */
class FakeQuery {
  readonly requests: BackfillRequest[] = [];
  readonly #gates: Gate[] = [];

  readonly fetch: BackfillFetch = request => {
    this.requests.push(request);
    const pending = gate();
    this.#gates.push(pending);
    return pending.promise;
  };

  /** The bounds of the `index`th range asked for, exclusive on both ends. */
  range(index = 0): readonly [number, number] {
    const request = this.requests[index];
    if (request === undefined) throw new Error(`no range was asked for at ${index}`);
    return [request.afterSequence, request.beforeSequence];
  }

  answer(events: readonly RunEvent[]): void {
    this.#latest().answer(events);
  }

  fail(error: Error): void {
    this.#latest().fail(error);
  }

  #latest(): Gate {
    const pending = this.#gates.at(-1);
    if (pending === undefined) throw new Error('no request is in flight');
    return pending;
  }
}

/** One backfill whose fetch answers immediately with `events`. */
function backfilled(
  reconciler: StreamReconciler,
  events: readonly RunEvent[] = [],
): Promise<BackfillOutcome> {
  return reconciler.backfill(() => Promise.resolve(events));
}

/** How far apart the run-level spine's events sit in a log. */
const SPINE_STRIDE = 120;

/**
 * The run-level spine a tail bootstrap replays from below `floor`: the run's
 * opening, one event per round through the history the tail cut off, and the
 * floor itself (`checkpoint_locked(floor, bootstrap_spine=True)`).
 */
function spineOf(floor: number): readonly number[] {
  if (floor === 0) return [];
  const spine = [1];
  for (let sequence = SPINE_STRIDE; sequence < floor; sequence += SPINE_STRIDE) {
    spine.push(sequence);
  }
  spine.push(floor);
  return [...new Set(spine)];
}

/** A bootstrap batch: the run-level spine from below `floor`, then the tail above it. */
function bootstrap(
  storeId: string | undefined,
  floor: number,
  tail: readonly number[],
): {readonly message: EventBatchMessage; readonly spine: readonly number[]} {
  const spine = spineOf(floor);
  const message: EventBatchMessage = {
    type: 'event_batch',
    events: [...spine.map(s => event(s, SPINE_TYPE)), ...tail.map(s => event(s))],
    history_after_sequence: floor,
  };
  if (storeId !== undefined) message.store_id = storeId;
  return {message, spine};
}

describe('StreamReconciler batch dispositions', () => {
  it('never re-bootstraps the first batch, whatever floor and store it declares', () => {
    const specs: readonly BatchSpec[] = [
      {},
      {storeId: 'log-a', declaredFloor: 900},
      {declaredFloor: 5_000},
      {storeId: ''},
    ];
    for (const spec of specs) {
      const reconciler = new StreamReconciler({backfillChunk: 100});
      expect(reconciler.reconcileBatch(batch(spec), FRESH)).toEqual({
        kind: 'extend',
        historyFloor: spec.declaredFloor ?? 0,
      });
    }
  });

  it('re-bootstraps a fresh batch that names a different store', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 40}), FRESH);
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 40}), FRESH),
    ).toEqual({kind: 'rebootstrap', historyFloor: 40});
    expect(reconciler.storeId()).toBe('run-log');
  });

  it('re-bootstraps a fresh batch declaring more history than the stream has declared', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH);
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), FRESH)).toEqual({
      kind: 'rebootstrap',
      historyFloor: 900,
    });
  });

  it('extends a fresh batch that re-declares the floor the stream already declared', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH);
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 100,
    });
  });

  it('keeps a backfilled floor when a later live batch re-declares the bootstrap floor', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 1_000});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), FRESH);
    expect(await backfilled(reconciler)).toEqual({
      kind: 'prepend',
      events: [],
      historyFloor: 500,
    });

    // The subscription re-declares its own bootstrap floor on every live batch.
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), FRESH)).toEqual(
      {
        kind: 'extend',
        historyFloor: 500,
      },
    );
  });

  it('takes a re-bootstrapped floor literally when it rises', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 0}), FRESH);
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 4_200}), FRESH),
    ).toEqual({kind: 'rebootstrap', historyFloor: 4_200});
  });

  it('takes a re-bootstrapped floor literally when a shorter run log replays whole', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 2_000}), FRESH);
    // A run log shorter than the tail is replayed whole and declares floor 0.
    // That is the truth about the log now streaming, not a backfill descent.
    expect(reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 0}), FRESH)).toEqual(
      {
        kind: 'rebootstrap',
        historyFloor: 0,
      },
    );
    expect(await backfilled(reconciler)).toEqual({kind: 'complete'});
  });

  /**
   * `PersistentEventStream` resumes only from a cursor a bootstrap batch
   * established, so this ordering does not occur on a live stream. It is
   * pinned because it is the one case where "the floor reached" and "the floor
   * declared" are both undefined: a resume before any bootstrap declares
   * nothing, so the next fresh batch is still a first batch and takes its own
   * floor literally rather than as a descent.
   */
  it('lets a resume before any bootstrap declare no floor at all', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), RESUMED)).toEqual(
      {
        kind: 'extend',
        historyFloor: 0,
      },
    );
    expect(await backfilled(reconciler)).toEqual({kind: 'complete'});
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 900,
    });
  });

  it('keeps the floor already reached on a resume so scrollback survives a reconnect', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 1_000});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), FRESH);
    await backfilled(reconciler);
    // A tail-less subscription withheld nothing, so it reports floor 0 however
    // far the cursor it resumed from had reached. Either way the floor the
    // reader backfilled to is what survives.
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0}), RESUMED)).toEqual({
      kind: 'extend',
      historyFloor: 500,
    });
  });

  /**
   * The resumed path reads the floor but never records it as declared, outside
   * the store-change branch below. Recording it would swallow the next real
   * re-bootstrap if a resume ever declared a floor above a tail dial's.
   */
  it('lets a resume declare any floor without arming the re-bootstrap signal', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH);
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 9_000}), RESUMED);
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 5_000}), FRESH)).toEqual(
      {
        kind: 'rebootstrap',
        historyFloor: 5_000,
      },
    );
  });

  it('treats a resumed batch from a different store as a fresh bootstrap, spine included', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const before = bootstrap('server-log', 100, [101, 102]);
    reconciler.reconcileBatch(before.message, FRESH);
    const swapped = bootstrap('run-log', 60, [61]);
    expect(reconciler.reconcileBatch(swapped.message, RESUMED)).toEqual({
      kind: 'rebootstrap',
      historyFloor: 60,
    });
    expect(reconciler.storeId()).toBe('run-log');
    // The new store's spine is tracked, and the superseded store's is gone.
    const settled = await backfilled(
      reconciler,
      [...swapped.spine, ...before.spine].map(sequence => event(sequence, SPINE_TYPE)),
    );
    expect(sequences(prepended(settled).events)).toEqual(
      before.spine.filter(sequence => !swapped.spine.includes(sequence)),
    );
  });

  /**
   * The store-change branch is the one place a resume writes `#declaredFloor`,
   * and on the wire the value it writes is 0: a tail-less subscription reports
   * `history_after_sequence = 0` whatever cursor it resumed from
   * (`reported_floor = 0 if request.tail is None`,
   * `src/server/transport/websocket.py:390`,
   * `src/server/transport/unix_jsonl.py:190`), because nothing was withheld.
   * The swap is a whole-log replay from zero (`src/server/api/service.py:372`),
   * so 0 is the truth about it, but every later tail dial then declares more
   * than 0 and re-bootstraps again with the store unchanged. TUI-identical
   * (`session-controller.ts:1436`), and pinned because `#declaredFloor`'s doc
   * comment claims it.
   */
  it('records the floor a resumed store swap declared, so the next tail dial re-bootstraps', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 2_000}), FRESH);
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 0}), RESUMED),
    ).toEqual({kind: 'rebootstrap', historyFloor: 0});
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 1_500}), FRESH),
    ).toEqual({kind: 'rebootstrap', historyFloor: 1_500});
  });
});

describe('StreamReconciler validation', () => {
  it('accepts an omitted declared floor as zero', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    expect(reconciler.reconcileBatch(batch({storeId: 'log'}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 0,
    });
  });

  /**
   * A `RangeError` rather than a `BackendClientError`: an out-of-range option
   * is a bug in the wiring code, not an outcome of talking to a server, so
   * there is no `BackendErrorKind` for a caller to branch on. See the note on
   * `BackendClientError`.
   */
  it('rejects a backfill chunk that cannot size a range', () => {
    for (const backfillChunk of [0, -1, 2.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => new StreamReconciler({backfillChunk})).toThrow(RangeError);
    }
  });
});

describe('StreamReconciler store identity', () => {
  it('reports an empty store id until a batch names one', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    expect(reconciler.storeId()).toBe('');
    reconciler.reconcileBatch(batch({declaredFloor: 10}), FRESH);
    expect(reconciler.storeId()).toBe('');
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), FRESH);
    expect(reconciler.storeId()).toBe('log');
  });

  it('leaves the declared floor as the only re-bootstrap signal with no reported identity', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({declaredFloor: 100}), FRESH);
    // The same (empty) identity on both sides, so only a raised floor can say so.
    expect(reconciler.reconcileBatch(batch({declaredFloor: 100}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 100,
    });
    expect(reconciler.reconcileBatch(batch({declaredFloor: 700}), FRESH)).toEqual({
      kind: 'rebootstrap',
      historyFloor: 700,
    });
  });

  it('compares an empty declared store against an empty known store on the fresh path', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: '', declaredFloor: 10}), FRESH);
    expect(reconciler.reconcileBatch(batch({storeId: '', declaredFloor: 10}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 10,
    });
  });

  it('keeps the last named store across a resume that names none', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), FRESH);
    expect(reconciler.reconcileBatch(batch({declaredFloor: 10}), RESUMED)).toEqual({
      kind: 'extend',
      historyFloor: 10,
    });
    expect(reconciler.storeId()).toBe('log');
  });

  /**
   * Pinned as the TUI's current behavior, not as a property worth having.
   *
   * The resumed path suppresses store-change detection when either side is
   * empty; the fresh path does not. So a server that stops reporting identity
   * mid-stream re-bootstraps a fresh batch and extends a resumed one, from the
   * same pair of payloads, and the fresh path also forgets the name it knew.
   * Preserved by the extraction deliberately. Reported as a candidate
   * follow-up alongside #869, which owns the server side of this matrix.
   */
  it('decides an emptied store id differently on the fresh and resumed paths (current behavior)', () => {
    const onFresh = new StreamReconciler({backfillChunk: 100});
    const onResume = new StreamReconciler({backfillChunk: 100});
    const named = batch({storeId: 'log', declaredFloor: 10});
    const anonymous = batch({declaredFloor: 10});
    onFresh.reconcileBatch(named, FRESH);
    onResume.reconcileBatch(named, FRESH);

    expect(onFresh.reconcileBatch(anonymous, FRESH)).toEqual({
      kind: 'rebootstrap',
      historyFloor: 10,
    });
    expect(onResume.reconcileBatch(anonymous, RESUMED)).toEqual({
      kind: 'extend',
      historyFloor: 10,
    });
    expect(onFresh.storeId()).toBe('');
    expect(onResume.storeId()).toBe('log');
  });

  /**
   * Also pinned as current behavior. `#declaredFloor` holds the floor the last
   * fresh batch declared, not the highest one the stream ever declared, so a
   * store-preserving descent is taken as a backfill descent (no discard, no
   * spine clear, no generation bump) and the next batch back at the original
   * floor re-bootstraps instead.
   *
   * What keeps the pair unreachable today is a client policy, not the server's
   * arithmetic: one subscription's floor is fixed at its dial, so a lower floor
   * needs a second bootstrap dial, and `PersistentEventStream` resumes on every
   * reconnect once a fresh batch has landed
   * (`persistent-event-stream.ts:250` and `:286`). Any consumer that dials
   * differently reaches it. #1036 owns the choice between clamping and
   * reporting.
   */
  it('takes a store-preserving floor descent as a backfill descent, then re-bootstraps on the way back up (current behavior)', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH);
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 50}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 50,
    });
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH)).toEqual({
      kind: 'rebootstrap',
      historyFloor: 100,
    });
  });
});

describe('StreamReconciler backfill', () => {
  it('reports history complete before any batch and at floor zero', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const query = new FakeQuery();
    expect(await reconciler.backfill(query.fetch)).toEqual({kind: 'complete'});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0}), FRESH);
    expect(await reconciler.backfill(query.fetch)).toEqual({kind: 'complete'});
    expect(query.requests).toEqual([]);
  });

  it('asks for the chunk below the floor, including the floor itself', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 40});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH);
    const query = new FakeQuery();
    // Both bounds are exclusive, so the range holds exactly `backfillChunk`
    // sequences and the last of them is the floor itself.
    const first = reconciler.backfill(query.fetch);
    expect(query.range(0)).toEqual([60, 101]);
    query.answer([]);
    await first;
    const second = reconciler.backfill(query.fetch);
    expect(query.range(1)).toEqual([20, 61]);
    query.answer([]);
    await second;
  });

  it('never asks below the start of the log', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 1_000});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), FRESH);
    const query = new FakeQuery();
    const settled = reconciler.backfill(query.fetch);
    expect(query.range()).toEqual([0, 11]);
    query.answer([]);
    expect(prepended(await settled).historyFloor).toBe(0);
  });

  it('shares one round trip between concurrent readers', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const query = new FakeQuery();
    const first = reconciler.backfill(query.fetch);
    const second = reconciler.backfill(query.fetch);
    // The floor moves only when an answer is folded, so a second range taken
    // now would be the same range. The second reader joins the first round trip
    // instead of asking for it again.
    expect(second).toBe(first);
    expect(query.requests).toHaveLength(1);
    query.answer([event(450)]);
    expect(prepended(await second).historyFloor).toBe(400);
    // The slot is free once the round trip settles, and the next ask moves on.
    const next = reconciler.backfill(query.fetch);
    expect(query.range(1)).toEqual([300, 401]);
    query.answer([]);
    await next;
  });

  it('frees the slot and leaves the floor when the fetch fails', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const query = new FakeQuery();
    const failed = reconciler.backfill(query.fetch);
    query.fail(new Error('query.events did not answer'));
    // The rejection reaches the caller, which is the only party that can decide
    // whether a failed backfill is worth showing.
    expect((await rejected(failed)).message).toBe('query.events did not answer');
    // The floor stayed where it was, so the same range is asked for again, and
    // the slot the failed request held was released without anyone reporting it.
    const retry = reconciler.backfill(query.fetch);
    expect(query.range(1)).toEqual(query.range(0));
    query.answer([event(450)]);
    expect(prepended(await retry).historyFloor).toBe(400);
  });

  it('frees the slot for a fetch that throws before it returns a promise', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const thrown = reconciler.backfill(() => {
      throw new Error('no transport');
    });
    expect((await rejected(thrown)).message).toBe('no transport');
    expect(prepended(await backfilled(reconciler, [])).historyFloor).toBe(400);
  });

  /**
   * The floor recorded comes from the range the reconciler computed when the
   * request left, not from wherever the floor has moved to by the time the
   * answer lands. Only a batch that lowers the floor without re-bootstrapping
   * can tell the two apart, and that is the store-preserving descent the server
   * does not send (pinned above as current behavior), so this is directed
   * rather than something the generated traffic reaches.
   */
  it('records the floor of the range it asked for, not the floor when the answer lands', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const query = new FakeQuery();
    const settled = reconciler.backfill(query.fetch);
    expect(query.range()).toEqual([400, 501]);
    // A descent with no re-bootstrap: same store, lower declared floor.
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 300}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 300,
    });
    query.answer([]);
    // 300, the lowest floor reached, and not the 200 that deriving the range
    // from the floor now held would give, which no round trip justifies.
    expect(prepended(await settled).historyFloor).toBe(300);
  });

  it('rejects an answer wholesale when a re-bootstrap landed while it was in flight', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 200}), FRESH);
    const query = new FakeQuery();
    const stale = reconciler.backfill(query.fetch);
    reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 900}), FRESH);
    query.answer([event(150), event(199)]);
    expect(await stale).toEqual({kind: 'superseded'});
  });

  it('leaves the floor where the re-bootstrap put it when it rejects a superseded answer', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 200}), FRESH);
    const query = new FakeQuery();
    const stale = reconciler.backfill(query.fetch);
    reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 900}), FRESH);
    query.answer([event(150)]);
    await stale;
    // The next ask backfills against the new log's own numbering, from 900.
    const next = reconciler.backfill(query.fetch);
    expect(query.range(1)).toEqual([800, 901]);
    query.answer([]);
    expect(prepended(await next).historyFloor).toBe(800);
  });

  it('reports history complete with a range still outstanding', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 400});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 300}), FRESH);
    const query = new FakeQuery();
    const outstanding = reconciler.backfill(query.fetch);
    // A live batch re-declaring floor zero says the log now streaming is
    // complete, so there is no older range left to wait for.
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0}), FRESH);
    expect(await reconciler.backfill(query.fetch)).toEqual({kind: 'complete'});
    expect(query.requests).toHaveLength(1);
    // The outstanding round trip still answers: nothing re-bootstrapped.
    query.answer([event(150)]);
    expect(await outstanding).toEqual({
      kind: 'prepend',
      events: [event(150)],
      historyFloor: 0,
    });
  });

  it('filters spine events the tail already delivered out of a chunk', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const {message, spine} = bootstrap('log', 200, [201, 202]);
    reconciler.reconcileBatch(message, FRESH);
    const chunk = [101, ...spine.filter(s => s >= 101), 150, 199].map(s => event(s, SPINE_TYPE));
    const settled = prepended(await backfilled(reconciler, chunk));
    expect(sequences(settled.events)).toEqual([101, 150, 199]);
    expect(settled.historyFloor).toBe(100);
  });

  /**
   * The two `sequence === undefined` branches (the spine filter's and
   * `#recordSpine`'s) are runtime no-ops: `Set.has(undefined)` is false and
   * `undefined <= 200` is false, so deleting either changes nothing any test
   * could see. `tsc` holds them instead (TS2345: `Set<number>.has` cannot take
   * `number | undefined`), which is why both are written as narrowing branches
   * rather than as disjuncts. What this pins is the observable half: an event
   * the server sent with no sequence is handed back rather than dropped, and
   * a sequenced one the batch already carried from below its floor is not.
   */
  it('hands back every unsequenced event in a chunk', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(
      batch({storeId: 'log', declaredFloor: 200, sequences: [undefined, 7]}),
      FRESH,
    );
    const settled = await backfilled(reconciler, [event(undefined), event(undefined), event(7)]);
    expect(sequences(prepended(settled).events)).toEqual([undefined, undefined]);
  });

  it('hands back an empty chunk as an empty prepend that still lowers the floor', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 200}), FRESH);
    expect(await backfilled(reconciler)).toEqual({
      kind: 'prepend',
      events: [],
      historyFloor: 100,
    });
  });

  /**
   * `#recordSpine` short-circuits on a declared floor of zero. A conforming
   * server cannot make that observable through a backfill: `after_sequence` is
   * `ge=0` and exclusive (`src/server/api/protocol.py:183-187`), so no range
   * can return sequence 0, and the only sequences the short circuit suppresses
   * are `<= 0`. It is observable through the filter being total over whatever
   * chunk the fetch answers with, which is what this pins, together with the
   * batch not disturbing a spine an earlier bootstrap recorded. Sequence 0 is
   * protocol-legal: `RunEvent.sequence` carries no lower bound
   * (`src/server/api/protocol.py:247`).
   */
  it('records no spine and disturbs none for a batch that declares floor zero', async () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const {message} = bootstrap('log', 200, [201]);
    reconciler.reconcileBatch(message, FRESH);
    const query = new FakeQuery();
    const settled = reconciler.backfill(query.fetch);
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0, sequences: [0]}), FRESH),
    ).toEqual({kind: 'extend', historyFloor: 0});
    query.answer([event(0), event(120, SPINE_TYPE), event(150)]);
    expect(sequences(prepended(await settled).events)).toEqual([0, 150]);
  });

  it('clears the spine set on a re-bootstrap but not on a backfill descent', async () => {
    const chunk = [event(1, SPINE_TYPE), event(50)];
    const {message} = bootstrap('log', 200, [201]);

    const descent = new StreamReconciler({backfillChunk: 100});
    descent.reconcileBatch(message, FRESH);
    await backfilled(descent);
    // The floor is 100 now; the spine recorded at 200 must still filter.
    const kept = prepended(await backfilled(descent, chunk));
    expect(sequences(kept.events)).toEqual([50]);

    const reset = new StreamReconciler({backfillChunk: 100});
    reset.reconcileBatch(message, FRESH);
    reset.reconcileBatch(batch({storeId: 'log', declaredFloor: 800}), FRESH);
    const unfiltered = prepended(await backfilled(reset, chunk));
    expect(sequences(unfiltered.events)).toEqual([1, 50]);
  });
});

// A deterministic 32-bit generator, so a failing seed is reproducible.
class Rng {
  #state: number;

  constructor(seed: number) {
    this.#state = (seed * 2654435761) >>> 0;
  }

  float(): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  }

  int(min: number, max: number): number {
    return min + Math.floor(this.float() * (max - min + 1));
  }

  pick<T>(values: readonly T[]): T {
    return values[this.int(0, values.length - 1)] as T;
  }

  chance(probability: number): boolean {
    return this.float() < probability;
  }
}

const SEEDS = [1, 7, 42, 271, 1_234, 90_210] as const;
/** Distinct event logs, each with its own sequence numbering starting at 1. */
const LOGS = ['server-log', 'run-log', 'run-log-2'] as const;
/** The bootstrap tail the generated stream dials with. */
const TAIL = 600;
const CHUNK = 400;

/** One command a client issues against the reconciler. */
type Command =
  | {readonly op: 'batch'; readonly resumed: boolean; readonly message: EventBatchMessage}
  | {readonly op: 'begin'; readonly id: number}
  | {readonly op: 'settle'; readonly id: number; readonly seed: number}
  | {readonly op: 'fail'; readonly id: number};

/**
 * The stream the generator is modelling, as the server sees it.
 *
 * A subscription's floor is fixed at its dial and reported on every batch it
 * sends, so the floor belongs to the connection rather than to a batch.
 */
interface Stream {
  /**
   * Whether this server reports store identity. One that does not predates the
   * field, and such a server has one store, so it never swaps.
   */
  readonly reportsIdentity: boolean;
  /** The log now streaming. Each log numbers its own sequences from 1. */
  log: string;
  /** The floor every batch on the live connection declares, as the wire carries it. */
  floor: number;
  /** Whether the live connection resumed from the client's cursor. */
  resumed: boolean;
  /** Whether a bootstrap batch has landed, so a resume has a cursor to use. */
  bootstrapped: boolean;
}

/**
 * Where a log currently ends. A log the client has not streamed yet has
 * history already, and every log outruns the tail bound, so a tail dial always
 * floors above zero. A log shorter than the tail replays whole and declares
 * floor zero, which the named tests pin directly; leaving it out here is what
 * keeps the backfill properties from going vacuous.
 */
function logTop(rng: Rng, tops: Map<string, number>, log: string): number {
  const existing = tops.get(log);
  if (existing !== undefined) return existing;
  const top = rng.int(TAIL + 200, TAIL + 1_800);
  tops.set(log, top);
  return top;
}

/** The sequences a batch appends, which the log has not carried before. */
function appended(rng: Rng, tops: Map<string, number>, log: string): readonly number[] {
  const tail: number[] = [];
  for (let index = 0; index < rng.int(0, 4); index += 1) {
    const next = logTop(rng, tops, log) + 1;
    tops.set(log, next);
    tail.push(next);
  }
  return tail;
}

function span(from: number, to: number): readonly number[] {
  const values: number[] = [];
  for (let value = from; value <= to; value += 1) values.push(value);
  return values;
}

/** A batch on the live connection: the floor it dialed with, plus what it carries. */
function batchOn(
  rng: Rng,
  tops: Map<string, number>,
  stream: Stream,
  redelivered: readonly number[],
): Command {
  const message: EventBatchMessage = {
    type: 'event_batch',
    events: [
      ...redelivered.map(sequence => event(sequence, SPINE_TYPE)),
      ...appended(rng, tops, stream.log).map(sequence => event(sequence)),
    ],
    history_after_sequence: stream.floor,
  };
  if (stream.reportsIdentity) message.store_id = stream.log;
  return {op: 'batch', resumed: stream.resumed, message};
}

/**
 * A tail bootstrap dial. The server floors the replay at `latest - tail` and
 * replays the run-level spine from below it
 * (`subscription_bootstrap`, `src/server/api/service.py:353-386`), and reports
 * that floor on every batch the connection sends
 * (`src/server/transport/websocket.py:390`).
 */
function bootstrapDial(rng: Rng, tops: Map<string, number>, stream: Stream): Command {
  if (stream.reportsIdentity && rng.chance(0.3)) stream.log = rng.pick(LOGS);
  stream.floor = Math.max(0, logTop(rng, tops, stream.log) - TAIL);
  stream.resumed = false;
  stream.bootstrapped = true;
  return batchOn(rng, tops, stream, spineOf(stream.floor));
}

/**
 * A resume dial: the client's cursor and no tail.
 *
 * A tail-less subscription reports `history_after_sequence = 0` on every batch
 * it sends, whatever cursor it resumed from
 * (`reported_floor = 0 if request.tail is None`,
 * `src/server/transport/websocket.py:390`,
 * `src/server/transport/unix_jsonl.py:190`): nothing was withheld, so there is
 * no floor to report. The cursor becomes `bootstrap.floor` internally
 * (`src/server/api/service.py:374`) and never reaches the client. No spine
 * either, because `bootstrap_spine = tail is not None`
 * (`src/server/api/service.py:376`, `src/server/journal.py:367`).
 *
 * A cursor naming a store the journal has since replaced is dropped and the
 * live log is replayed whole from zero (`src/server/api/service.py:372-374`).
 */
function resumeDial(rng: Rng, tops: Map<string, number>, stream: Stream): Command {
  const swapped = stream.reportsIdentity && rng.chance(0.3);
  if (swapped) stream.log = rng.pick(LOGS.filter(log => log !== stream.log));
  stream.floor = 0;
  stream.resumed = true;
  return batchOn(rng, tops, stream, swapped ? span(1, logTop(rng, tops, stream.log)) : []);
}

/**
 * One backfill answer, dense over the range the request asked for. Both bounds
 * are exclusive (`src/server/api/protocol.py:183-187`), so a chunk covering a
 * bootstrap floor redelivers the spine event sitting at it, which is what the
 * reconciler has to filter.
 */
function chunkForRange(request: BackfillRequest, seed: number): readonly RunEvent[] {
  const rng = new Rng(seed);
  // A range addressed in a log the journal has since replaced holds nothing.
  if (rng.chance(0.1)) return [];
  const events: RunEvent[] = [];
  for (let sequence = request.afterSequence + 1; sequence < request.beforeSequence; sequence += 1) {
    if (!rng.chance(0.05)) events.push(event(sequence, SPINE_TYPE));
  }
  if (rng.chance(0.2)) events.push(event(undefined, SPINE_TYPE));
  return events;
}

/**
 * A random command sequence shaped like real traffic: tail bootstrap dials
 * that replay the spine below their floor, live batches that re-declare the
 * same floor, resume dials with and without the store swapped underneath
 * them, log swaps against a server that reports identity and floor moves
 * against one that does not, and backfills that are asked for again while one
 * is outstanding, or fail.
 */
function generateCommands(rng: Rng, length: number): readonly Command[] {
  const tops = new Map<string, number>();
  const stream: Stream = {
    reportsIdentity: rng.chance(0.75),
    log: LOGS[0],
    floor: 0,
    resumed: false,
    bootstrapped: false,
  };
  const open: number[] = [];
  // `PersistentEventStream` resumes only from a cursor a bootstrap batch
  // established, so the first batch of a stream is always a fresh one.
  const commands: Command[] = [bootstrapDial(rng, tops, stream)];
  let issued = 0;
  for (let index = 1; index < length; index += 1) {
    if (open.length > 0 && rng.chance(0.3)) {
      const id = open.splice(rng.int(0, open.length - 1), 1)[0] as number;
      const seed = rng.int(1, 1 << 20);
      commands.push(rng.chance(0.25) ? {op: 'fail', id} : {op: 'settle', id, seed});
      continue;
    }
    if (rng.chance(0.3)) {
      commands.push({op: 'begin', id: issued});
      open.push(issued);
      issued += 1;
      continue;
    }
    const roll = rng.float();
    if (roll < 0.2) commands.push(bootstrapDial(rng, tops, stream));
    else if (roll < 0.4 && stream.bootstrapped) commands.push(resumeDial(rng, tops, stream));
    else commands.push(batchOn(rng, tops, stream, []));
  }
  return commands;
}

/** What one command decided, as the caller can observe it. */
type Observation =
  | {readonly op: 'batch'; readonly decision: BatchReconciliation}
  | {
      readonly op: 'begin';
      /**
       * `fetch` asked for a range. `joined` got the outstanding round trip's
       * own promise back instead of a second range. Anything else is what the
       * backfill resolved to without asking, which is `complete`.
       */
      readonly kind: 'fetch' | 'joined' | BackfillOutcome['kind'];
      readonly range: readonly [number, number] | null;
    }
  | {
      readonly op: 'settle';
      readonly kind: BackfillOutcome['kind'];
      readonly events: readonly (number | undefined)[];
      readonly historyFloor: number | null;
    }
  | {readonly op: 'fail'; readonly rejected: boolean; readonly detail: string};

/**
 * The reconciler's surface, so the differential oracle can be driven by the
 * same harness without either side knowing about the other.
 */
interface Reconciling {
  storeId(): string;
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation;
  backfill(fetch: BackfillFetch): Promise<BackfillOutcome>;
}

interface Trace {
  readonly observations: readonly Observation[];
  /** The sequences the caller ended up holding, standing in for its folded log. */
  readonly folded: readonly number[];
  /** Sequences a prepend handed back that the caller had already folded. */
  readonly refolded: readonly number[];
  readonly prepends: number;
  /** Events the spine filter dropped, each of which would have been a refold. */
  readonly filtered: number;
  /** Asks that joined the outstanding round trip rather than starting one. */
  readonly joins: number;
  /** Round trips whose fetch rejected. */
  readonly failures: number;
  readonly historyFloor: number;
  readonly storeId: string;
}

/** The one round trip a walk can have outstanding, and the gate that ends it. */
interface Pending {
  readonly gate: Gate;
  readonly outcome: Promise<BackfillOutcome>;
  readonly request: BackfillRequest;
}

/** The walk's mutable position, threaded through the per-command handlers. */
interface Walk {
  readonly reconciler: Reconciling;
  readonly folded: Set<number>;
  readonly refolded: number[];
  readonly observations: Observation[];
  pending: Pending | null;
  historyFloor: number;
  prepends: number;
  filtered: number;
  joins: number;
  failures: number;
  record: boolean;
}

async function walkBegin(walk: Walk): Promise<void> {
  const pending = gate();
  // An array rather than a `let`: the fetch runs inside `backfill`, and control
  // flow analysis would narrow a reassigned local back to its initializer.
  const asked: BackfillRequest[] = [];
  const outcome = walk.reconciler.backfill(request => {
    asked.push(request);
    return pending.promise;
  });
  const request = asked[0];
  if (request === undefined) {
    if (walk.pending !== null && walk.pending.outcome === outcome) {
      // A range is already outstanding, and this is that round trip's own
      // promise, so there is one answer for both readers and one fold.
      walk.joins += 1;
      if (walk.record) walk.observations.push({op: 'begin', kind: 'joined', range: null});
      return;
    }
    // Nothing was asked for and nothing is outstanding, so the fold already
    // reaches the start of the log and the answer is immediate.
    const settled = await outcome;
    if (walk.record) walk.observations.push({op: 'begin', kind: settled.kind, range: null});
    return;
  }
  walk.pending = {gate: pending, outcome, request};
  if (walk.record) {
    walk.observations.push({
      op: 'begin',
      kind: 'fetch',
      range: [request.afterSequence, request.beforeSequence],
    });
  }
}

async function walkSettle(walk: Walk, command: Extract<Command, {op: 'settle'}>): Promise<void> {
  const pending = walk.pending;
  if (pending === null) return;
  walk.pending = null;
  const chunk = chunkForRange(pending.request, command.seed);
  pending.gate.answer(chunk);
  const settled = await pending.outcome;
  if (settled.kind === 'prepend') {
    walk.prepends += 1;
    walk.filtered += chunk.length - settled.events.length;
    walk.historyFloor = settled.historyFloor;
    for (const item of settled.events) {
      if (item.sequence === undefined) continue;
      if (walk.folded.has(item.sequence)) walk.refolded.push(item.sequence);
      walk.folded.add(item.sequence);
    }
  }
  if (!walk.record) return;
  walk.observations.push({
    op: 'settle',
    kind: settled.kind,
    events: settled.kind === 'prepend' ? sequences(settled.events) : [],
    historyFloor: settled.kind === 'prepend' ? settled.historyFloor : null,
  });
}

async function walkFail(walk: Walk, command: Extract<Command, {op: 'fail'}>): Promise<void> {
  const pending = walk.pending;
  if (pending === null) return;
  walk.pending = null;
  const detail = `query.events failed (${command.id})`;
  pending.gate.fail(new Error(detail));
  // Recorded either way: a reconciler that swallowed the rejection and resolved
  // instead would show up here rather than as a missing observation.
  const settled = await pending.outcome.then(
    outcome => ({rejected: false, detail: outcome.kind}),
    error => ({rejected: true, detail: (error as Error).message}),
  );
  walk.failures += 1;
  if (walk.record) walk.observations.push({op: 'fail', ...settled});
}

function walkBatch(walk: Walk, command: Extract<Command, {op: 'batch'}>): void {
  const decision = walk.reconciler.reconcileBatch(command.message, {resumed: command.resumed});
  if (decision.kind === 'rebootstrap') walk.folded.clear();
  for (const item of command.message.events) {
    if (item.sequence !== undefined) walk.folded.add(item.sequence);
  }
  walk.historyFloor = decision.historyFloor;
  if (walk.record) walk.observations.push({op: 'batch', decision});
}

/**
 * Drives a reconciler the way a client does: it folds nothing, but it applies
 * each disposition to a set standing in for the folded log, records the floor
 * each disposition reports, and holds the one round trip that can be in flight.
 * Observations are recorded from `recordFrom` onwards.
 */
async function drive(
  reconciler: Reconciling,
  commands: readonly Command[],
  recordFrom = 0,
): Promise<Trace> {
  const walk: Walk = {
    reconciler,
    folded: new Set(),
    refolded: [],
    observations: [],
    pending: null,
    historyFloor: 0,
    prepends: 0,
    filtered: 0,
    joins: 0,
    failures: 0,
    record: recordFrom === 0,
  };
  for (const [index, command] of commands.entries()) {
    walk.record = index >= recordFrom;
    if (index === recordFrom) {
      // The counters describe the recorded window, not the warm-up before it.
      walk.refolded.length = 0;
      walk.prepends = 0;
      walk.filtered = 0;
      walk.joins = 0;
      walk.failures = 0;
    }
    if (command.op === 'begin') await walkBegin(walk);
    else if (command.op === 'settle') await walkSettle(walk, command);
    else if (command.op === 'fail') await walkFail(walk, command);
    else walkBatch(walk, command);
  }
  return {
    observations: walk.observations,
    folded: [...walk.folded].sort((left, right) => left - right),
    refolded: walk.refolded,
    prepends: walk.prepends,
    filtered: walk.filtered,
    joins: walk.joins,
    failures: walk.failures,
    historyFloor: walk.historyFloor,
    storeId: reconciler.storeId(),
  };
}

function floorOf(observation: Observation): number | null {
  if (observation.op === 'batch') return observation.decision.historyFloor;
  if (observation.op === 'settle') return observation.historyFloor;
  return null;
}

/** Each floor a disposition reported, and whether it came from a re-bootstrap. */
function floorSteps(
  observations: readonly Observation[],
): readonly {readonly floor: number; readonly rebootstrapped: boolean}[] {
  const steps: {floor: number; rebootstrapped: boolean}[] = [];
  for (const observation of observations) {
    const floor = floorOf(observation);
    if (floor === null) continue;
    const rebootstrapped =
      observation.op === 'batch' && observation.decision.kind === 'rebootstrap';
    steps.push({floor, rebootstrapped});
  }
  return steps;
}

/**
 * Every range asked for, paired with the floor the disposition before it
 * reported. That floor is what the range has to be derived from: the caller
 * never passes one in, so a range disagreeing with it is the reconciler having
 * lost track of its own floor.
 */
function askedRanges(
  observations: readonly Observation[],
): readonly {readonly range: readonly [number, number]; readonly floor: number}[] {
  const asked: {range: readonly [number, number]; floor: number}[] = [];
  let floor = 0;
  for (const observation of observations) {
    if (observation.op === 'begin') {
      if (observation.range !== null) asked.push({range: observation.range, floor});
      continue;
    }
    const reported = floorOf(observation);
    if (reported !== null) floor = reported;
  }
  return asked;
}

describe('StreamReconciler properties', () => {
  for (const seed of SEEDS) {
    it(`only ever raises the floor through a re-bootstrap (seed ${seed})`, async () => {
      const trace = await drive(
        new StreamReconciler({backfillChunk: CHUNK}),
        generateCommands(new Rng(seed), 100),
      );
      const steps = floorSteps(trace.observations);
      let previous: number | null = null;
      for (const {floor, rebootstrapped} of steps) {
        expect(floor).toBeGreaterThanOrEqual(0);
        if (previous !== null && !rebootstrapped) expect(floor).toBeLessThanOrEqual(previous);
        previous = floor;
      }
      expect(steps.filter(step => step.rebootstrapped)).not.toEqual([]);
    });

    it(`asks for exactly the chunk below the floor it last reported (seed ${seed})`, async () => {
      const trace = await drive(
        new StreamReconciler({backfillChunk: CHUNK}),
        generateCommands(new Rng(seed), 100),
      );
      const asked = askedRanges(trace.observations);
      for (const {range, floor} of asked) {
        // Both bounds exclusive, so the range spans `before - after - 1`
        // sequences: the chunk, clamped at the start of the log, ending at the
        // floor itself.
        expect(range).toEqual([Math.max(0, floor - CHUNK), floor + 1]);
      }
      expect(asked).not.toEqual([]);
      // Both of the backfill paths that are not a plain round trip occur in the
      // generated traffic, so the assertions above are not the only ones run.
      expect(trace.joins).toBeGreaterThan(0);
      expect(trace.failures).toBeGreaterThan(0);
    });

    it(`never hands back a backfilled event the caller already folded (seed ${seed})`, async () => {
      // `drive` applies every disposition to the set standing in for the folded
      // log and records any sequence a prepend handed back twice, which is what
      // folding a spine event a second time would look like.
      const trace = await drive(
        new StreamReconciler({backfillChunk: CHUNK}),
        generateCommands(new Rng(seed), 100),
      );
      expect(trace.refolded).toEqual([]);
      // The spine set only ever holds sequences the caller folded from the same
      // batch, and a re-bootstrap clears both together, so every event the
      // filter dropped is one the assertion above would have caught. A positive
      // count is therefore exactly the claim that the property is not vacuous.
      expect(trace.filtered).toBeGreaterThan(0);
    });

    /**
     * With nothing in flight. A round trip outstanding across the re-bootstrap
     * deliberately survives it (the TUI's `#historyFetch` does too), so the
     * prefix is drained first: at most one is outstanding, and failing one that
     * is not there is a no-op.
     */
    it(`makes a re-bootstrap independent of everything before it (seed ${seed})`, async () => {
      // A fresh batch naming a log the generator never picks always
      // re-bootstraps once any batch has landed, whatever the prefix left.
      const cut: Command = {
        op: 'batch',
        resumed: false,
        message: {
          type: 'event_batch',
          store_id: 'attached-run-log',
          history_after_sequence: 640,
          events: [1, 320, 640, 641, 642].map(s => event(s, s <= 640 ? SPINE_TYPE : TAIL_TYPE)),
        },
      };
      const seeded: Command = {
        op: 'batch',
        resumed: false,
        message: batch({storeId: 'seed-log', declaredFloor: 10}),
      };
      const after = generateCommands(new Rng(seed + 2), 30);
      const drain: readonly Command[] = [{op: 'fail', id: -1}];
      const trails: Trace[] = [];
      for (const length of [20, 45]) {
        const prefix = generateCommands(new Rng(seed + length), length);
        const commands = [...prefix, ...drain, seeded, cut, ...after];
        trails.push(
          await drive(
            new StreamReconciler({backfillChunk: CHUNK}),
            commands,
            prefix.length + drain.length + 1,
          ),
        );
      }
      expect(trails[0]?.observations[0]).toEqual({
        op: 'batch',
        decision: {kind: 'rebootstrap', historyFloor: 640},
      });
      expect(trails[1]).toEqual(trails[0] as Trace);
    });

    /**
     * At every position in the trace, not only at its end: an ending floor of
     * zero makes `declared > #declaredFloor` unsatisfiable, so the claim would
     * hold there for any implementation that echoes the declared floor back.
     * Those positions are counted instead of asserted, and the count is what
     * says the rest were real comparisons.
     */
    it(`treats a batch repeating what the stream already said as a no-op (seed ${seed})`, async () => {
      const commands = generateCommands(new Rng(seed), 40);
      let probed = 0;
      for (const length of span(1, commands.length)) {
        const reconciler = new StreamReconciler({backfillChunk: CHUNK});
        const trace = await drive(reconciler, commands.slice(0, length));
        if (trace.historyFloor === 0) continue;
        probed += 1;
        // The floor the caller holds is at or below the floor the stream last
        // declared, so a batch re-declaring it is neither a raise nor a descent,
        // on either path.
        const repeat = batch({storeId: trace.storeId, declaredFloor: trace.historyFloor});
        expect(reconciler.reconcileBatch(repeat, FRESH)).toEqual({
          kind: 'extend',
          historyFloor: trace.historyFloor,
        });
        expect(reconciler.reconcileBatch(repeat, RESUMED)).toEqual({
          kind: 'extend',
          historyFloor: trace.historyFloor,
        });
      }
      expect(probed).toBeGreaterThan(0);
    });
  }
});

/**
 * The TUI controller's reconciliation, transcribed from
 * `clients/tui/src/session-controller.ts` at merge base 667a08f8.
 *
 * Kept in the controller's own shape rather than the reconciler's: a resumed
 * and a fresh batch method, the floor and spine helpers, the `#historyFetch`
 * promise released by a `finally`, and the floor read back out of the state the
 * controller wrote. It is a transliteration, not an independent derivation, so
 * it does not confirm the extraction is right. It is regression protection: it
 * fails loudly if the controller and the reconciler drift before phase (b)
 * migrates the controller onto this. The equivalence argument is the hand
 * re-derivation recorded in the PR body.
 *
 * Line references in that file:
 *
 * - fields: 286 `#historyFetch`, 299 `#foldedBelowFloor`, 301 `#historyFloor`,
 *   310 `#declaredFloor`, 319 `#storeId`, 328 `#rebootstrapGeneration`
 * - `storeId` subscription callback: 381
 * - `loadOlderHistory`: 425-433, `#requestOlderHistory`: 435-465
 * - `#reconcileBatch`: 1419-1425, `#reconcileResumedBatch`: 1427-1460,
 *   `#reconcileFreshBatch`: 1462-1483
 * - `#lowerHistoryFloor`: 1493-1496, `#resetHistoryFloor`: 1509-1514,
 *   `#recordSpine`: 1517-1525
 *
 * Two things are deliberately not transcribed, because they are where this
 * phase differs from the controller rather than drift:
 *
 * - the declared-floor validation, which is new and lives in
 *   `parseServerMessage` rather than in either of these. The generator only
 *   produces floors a conforming server can send, so it would never fire in a
 *   differential run anyway.
 * - the `catch` at 459-464, which turns a failed request into a state report
 *   and a `false`. Whether a failure is worth showing is the caller's decision,
 *   so the reconciler lets the rejection through and the oracle does the same.
 */
class ControllerOracle implements Reconciling {
  readonly #backfillChunk: number;
  readonly #foldedBelowFloor = new Set<number>();
  #historyFloor = Number.POSITIVE_INFINITY;
  #declaredFloor: number | null = null;
  #storeId: string | null = null;
  #rebootstrapGeneration = 0;
  /** `#historyFetch`, the single-flight guard, released by the `finally` at 428-430. */
  #historyFetch: Promise<BackfillOutcome> | null = null;
  /**
   * `this.#state.core.historyAfterSequence`. The controller reads the floor
   * back out of the state it wrote (426 and 436 read it, 457, 1437, 1452 and
   * 1474 write it); the reconciler keeps its own field instead. Nothing in the
   * harness feeds a floor to either side, because neither `reconcileBatch` nor
   * `backfill` takes one, so the two floors are maintained independently from
   * the same commands and comparing the traces is what checks they agree at
   * every step.
   */
  #stateFloor = 0;

  constructor(backfillChunk: number) {
    this.#backfillChunk = backfillChunk;
  }

  // Line 381.
  storeId(): string {
    return this.#storeId ?? '';
  }

  // Lines 1419-1425.
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation {
    const decision = context.resumed
      ? this.#reconcileResumedBatch(message, this.#stateFloor)
      : this.#reconcileFreshBatch(message);
    this.#stateFloor = decision.historyFloor;
    return decision;
  }

  // Lines 1427-1460.
  #reconcileResumedBatch(message: EventBatchMessage, stateFloor: number): BatchReconciliation {
    const store = message.store_id ?? '';
    const knownStoreChanged = Boolean(this.#storeId && store && store !== this.#storeId);
    if (knownStoreChanged) {
      const declared = message.history_after_sequence ?? 0;
      this.#storeId = store;
      this.#declaredFloor = declared;
      const floor = this.#resetHistoryFloor(declared);
      this.#recordSpine(message.events, declared);
      return {kind: 'rebootstrap', historyFloor: floor};
    }
    if (store) this.#storeId = store;
    return {kind: 'extend', historyFloor: stateFloor};
  }

  // Lines 1462-1483.
  #reconcileFreshBatch(message: EventBatchMessage): BatchReconciliation {
    const declared = message.history_after_sequence ?? 0;
    const store = message.store_id ?? '';
    const rebootstrap =
      (this.#storeId !== null && store !== this.#storeId) ||
      (this.#declaredFloor !== null && declared > this.#declaredFloor);
    this.#storeId = store;
    this.#declaredFloor = declared;
    const floor = rebootstrap
      ? this.#resetHistoryFloor(declared)
      : this.#lowerHistoryFloor(declared);
    this.#recordSpine(message.events, declared);
    return {kind: rebootstrap ? 'rebootstrap' : 'extend', historyFloor: floor};
  }

  // Lines 425-433.
  backfill(fetch: BackfillFetch): Promise<BackfillOutcome> {
    if (this.#stateFloor === 0) return Promise.resolve({kind: 'complete'});
    if (this.#historyFetch !== null) return this.#historyFetch;
    const running = this.#requestOlderHistory(fetch).finally(() => {
      this.#historyFetch = null;
    });
    this.#historyFetch = running;
    return running;
  }

  // Lines 435-458.
  async #requestOlderHistory(fetch: BackfillFetch): Promise<BackfillOutcome> {
    const floor = this.#stateFloor;
    const nextFloor = Math.max(0, floor - this.#backfillChunk);
    const generation = this.#rebootstrapGeneration;
    const events = await fetch({afterSequence: nextFloor, beforeSequence: floor + 1});
    if (this.#rebootstrapGeneration !== generation) return {kind: 'superseded'};
    const filtered = events.filter(
      item => item.sequence === undefined || !this.#foldedBelowFloor.has(item.sequence),
    );
    this.#stateFloor = this.#lowerHistoryFloor(nextFloor);
    return {kind: 'prepend', events: filtered, historyFloor: this.#stateFloor};
  }

  // Lines 1493-1496.
  #lowerHistoryFloor(floor: number): number {
    this.#historyFloor = Math.min(this.#historyFloor, floor);
    return this.#historyFloor;
  }

  // Lines 1509-1514.
  #resetHistoryFloor(floor: number): number {
    this.#rebootstrapGeneration += 1;
    this.#historyFloor = floor;
    this.#foldedBelowFloor.clear();
    return floor;
  }

  // Lines 1517-1525.
  #recordSpine(events: readonly RunEvent[], historyAfterSequence: number): void {
    if (historyAfterSequence === 0) return;
    for (const {sequence} of events) {
      if (sequence !== undefined && sequence <= historyAfterSequence) {
        this.#foldedBelowFloor.add(sequence);
      }
    }
  }
}

describe('StreamReconciler against the TUI controller', () => {
  for (const seed of SEEDS) {
    it(`decides every command exactly as the controller does (seed ${seed})`, async () => {
      const commands = generateCommands(new Rng(seed), 150);
      expect(await drive(new StreamReconciler({backfillChunk: CHUNK}), commands)).toEqual(
        await drive(new ControllerOracle(CHUNK), commands),
      );
    });
  }

  it('agrees across the whole store identity and floor matrix', async () => {
    const payloads: readonly EventBatchMessage[] = [
      batch({storeId: 'log', declaredFloor: 100, sequences: [50, 100, 101]}),
      batch({storeId: '', declaredFloor: 100, sequences: [102]}),
      batch({declaredFloor: 100, sequences: [103]}),
      batch({sequences: [104]}),
      batch({storeId: 'log', declaredFloor: 0, sequences: [1]}),
      batch({storeId: 'log', declaredFloor: 40, sequences: [20, 40, 41]}),
      batch({storeId: 'other', declaredFloor: 100, sequences: [50, 100]}),
      batch({storeId: 'other', declaredFloor: 900, sequences: [900, 901]}),
    ];
    // Probes the state the pair left behind: the store known and the floor last
    // declared, neither of which the dispositions above report directly.
    const probe: Command = {
      op: 'batch',
      resumed: false,
      message: batch({storeId: 'log', declaredFloor: 5_000}),
    };
    for (const resumed of [false, true]) {
      for (const first of payloads) {
        for (const second of payloads) {
          const commands: readonly Command[] = [
            {op: 'batch', resumed: false, message: first},
            {op: 'begin', id: 0},
            {op: 'batch', resumed, message: second},
            {op: 'settle', id: 0, seed: 17},
            {op: 'begin', id: 1},
            {op: 'fail', id: 1},
            {op: 'begin', id: 2},
            {op: 'settle', id: 2, seed: 23},
            probe,
            {op: 'begin', id: 3},
            {op: 'begin', id: 4},
            {op: 'settle', id: 3, seed: 29},
          ];
          expect(await drive(new StreamReconciler({backfillChunk: 60}), commands)).toEqual(
            await drive(new ControllerOracle(60), commands),
          );
        }
      }
    }
  });
});
