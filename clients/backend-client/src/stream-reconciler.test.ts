import {describe, expect, it} from 'bun:test';
import {BackendClientError} from './errors.js';
import type {EventBatchMessage, RunEvent} from './protocol.js';
import {
  type BackfillPlan,
  type BackfillReconciliation,
  type BackfillRequest,
  type BackfillStamp,
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

function prepended(settled: BackfillReconciliation): {
  readonly events: readonly RunEvent[];
  readonly historyFloor: number;
} {
  if (settled.kind !== 'prepend') throw new Error(`expected a prepend, got ${settled.kind}`);
  return settled;
}

function fetched(plan: BackfillPlan): BackfillRequest {
  if (plan.kind !== 'fetch') throw new Error(`expected a range to fetch, got ${plan.kind}`);
  return plan.request;
}

/** The rejection `action` threw, so a test can assert on its kind and message. */
function rejection(action: () => unknown): BackendClientError {
  try {
    action();
  } catch (error) {
    if (error instanceof BackendClientError) return error;
    throw error;
  }
  throw new Error('expected a rejection');
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

  it('keeps a backfilled floor when a later live batch re-declares the bootstrap floor', () => {
    const reconciler = new StreamReconciler({backfillChunk: 1_000});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), FRESH);
    const settled = reconciler.settleBackfill(fetched(reconciler.beginBackfill()), []);
    expect(settled).toEqual({kind: 'prepend', events: [], historyFloor: 500});

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

  it('takes a re-bootstrapped floor literally when a shorter run log replays whole', () => {
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
    expect(reconciler.beginBackfill()).toEqual({kind: 'complete'});
  });

  /**
   * `PersistentEventStream` resumes only from a cursor a bootstrap batch
   * established, so this ordering does not occur on a live stream. It is
   * pinned because it is the one case where "the floor reached" and "the floor
   * declared" are both undefined: a resume before any bootstrap declares
   * nothing, so the next fresh batch is still a first batch and takes its own
   * floor literally rather than as a descent.
   */
  it('lets a resume before any bootstrap declare no floor at all', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), RESUMED)).toEqual(
      {
        kind: 'extend',
        historyFloor: 0,
      },
    );
    expect(reconciler.beginBackfill()).toEqual({kind: 'complete'});
    expect(reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 900,
    });
  });

  it('keeps the floor already reached on a resume so scrollback survives a reconnect', () => {
    const reconciler = new StreamReconciler({backfillChunk: 1_000});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), FRESH);
    reconciler.settleBackfill(fetched(reconciler.beginBackfill()), []);
    // A resume dials with no tail, so the server declares the client's cursor
    // as the floor. That is far above the 500 the reader has backfilled to.
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 9_000}), RESUMED),
    ).toEqual({kind: 'extend', historyFloor: 500});
  });

  /**
   * The resumed path reads the floor but never records it as declared. The
   * cursor a resume declares is the top of the caller's fold, far above any
   * bootstrap floor, so recording it would swallow the next real
   * re-bootstrap: every later tail dial declares less than the cursor.
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

  it('treats a resumed batch from a different store as a fresh bootstrap, spine included', () => {
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
    const settled = reconciler.settleBackfill(
      fetched(reconciler.beginBackfill()),
      [...swapped.spine, ...before.spine].map(sequence => event(sequence, SPINE_TYPE)),
    );
    expect(sequences(prepended(settled).events)).toEqual(
      before.spine.filter(sequence => !swapped.spine.includes(sequence)),
    );
  });
});

describe('StreamReconciler validation', () => {
  it('rejects a declared floor that cannot be a sequence, naming the key', () => {
    for (const declaredFloor of [-1, -4_096, 12.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      const reconciler = new StreamReconciler({backfillChunk: 100});
      const error = rejection(() => reconciler.reconcileBatch(batch({declaredFloor}), FRESH));
      expect(error.kind).toBe('parse');
      expect(error.message).toContain('event_batch.history_after_sequence');
      expect(error.message).toContain(String(declaredFloor));
    }
  });

  it('rejects a declared floor on the resumed path too, which ignores its value', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), FRESH);
    expect(
      rejection(() => reconciler.reconcileBatch(batch({declaredFloor: -1}), RESUMED)).kind,
    ).toBe('parse');
  });

  it('accepts an omitted declared floor as zero', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    expect(reconciler.reconcileBatch(batch({storeId: 'log'}), FRESH)).toEqual({
      kind: 'extend',
      historyFloor: 0,
    });
  });

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
   * floor re-bootstraps instead. The server never sends that pair: one
   * subscription's floor is fixed at its dial, so a lower floor means a new
   * dial, and a new dial's floor only rises as the log grows. Reported as a
   * candidate follow-up.
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
  it('reports history complete before any batch and at floor zero', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    expect(reconciler.beginBackfill()).toEqual({kind: 'complete'});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0}), FRESH);
    expect(reconciler.beginBackfill()).toEqual({kind: 'complete'});
  });

  it('asks for the chunk below the floor, including the floor itself', () => {
    const reconciler = new StreamReconciler({backfillChunk: 40});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), FRESH);
    // Both bounds are exclusive, so the range holds exactly `backfillChunk`
    // sequences and the last of them is the floor itself.
    const first = fetched(reconciler.beginBackfill());
    expect([first.afterSequence, first.beforeSequence]).toEqual([60, 101]);
    reconciler.settleBackfill(first, []);
    const second = fetched(reconciler.beginBackfill());
    expect([second.afterSequence, second.beforeSequence]).toEqual([20, 61]);
  });

  it('never asks below the start of the log', () => {
    const reconciler = new StreamReconciler({backfillChunk: 1_000});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    expect([request.afterSequence, request.beforeSequence]).toEqual([0, 11]);
    expect(prepended(reconciler.settleBackfill(request, [])).historyFloor).toBe(0);
  });

  it('holds one range at a time until the caller settles it', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    // The floor moves only when a response is folded, so a second range taken
    // now would be the same range.
    expect(reconciler.beginBackfill()).toEqual({kind: 'in-flight'});
    reconciler.settleBackfill(request, []);
    expect(fetched(reconciler.beginBackfill()).afterSequence).toBe(300);
  });

  it('folds one response once, so a duplicated answer cannot lower the floor twice', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    expect(prepended(reconciler.settleBackfill(request, [event(450)])).historyFloor).toBe(400);
    expect(reconciler.settleBackfill(request, [event(450)])).toEqual({kind: 'superseded'});
  });

  it('retries the same range after an abandoned request', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const failed = fetched(reconciler.beginBackfill());
    reconciler.abandonBackfill(failed);
    const retry = fetched(reconciler.beginBackfill());
    expect([retry.afterSequence, retry.beforeSequence]).toEqual([
      failed.afterSequence,
      failed.beforeSequence,
    ]);
    // The abandoned request is no longer outstanding, so a late answer to it
    // cannot fold under the retry.
    expect(reconciler.settleBackfill(failed, [event(450)])).toEqual({kind: 'superseded'});
    expect(prepended(reconciler.settleBackfill(retry, [event(450)])).historyFloor).toBe(400);
  });

  it('ignores an abandon of a range that is not outstanding', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const first = fetched(reconciler.beginBackfill());
    reconciler.settleBackfill(first, []);
    const second = fetched(reconciler.beginBackfill());
    reconciler.abandonBackfill(first);
    expect(prepended(reconciler.settleBackfill(second, [])).historyFloor).toBe(300);
  });

  it('judges a response by the range it issued, not the one handed back', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    // A widened copy keeps the stamp, so it settles, but the floor comes from
    // the range the reconciler issued rather than the one it was handed.
    expect(
      prepended(reconciler.settleBackfill({...request, afterSequence: 0}, [])).historyFloor,
    ).toBe(400);
  });

  it('cannot be handed a range it never issued', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 500}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    const forged: BackfillRequest = {...request, stamp: (request.stamp + 1) as BackfillStamp};
    expect(reconciler.settleBackfill(forged, [event(450)])).toEqual({kind: 'superseded'});
    // @ts-expect-error a stamp is only mintable inside the reconciler
    const invented: BackfillRequest = {...request, stamp: request.stamp + 1};
    reconciler.abandonBackfill(invented);
    // Neither touched the outstanding range.
    expect(prepended(reconciler.settleBackfill(request, [event(450)])).historyFloor).toBe(400);
  });

  it('reports history complete with a range still outstanding', () => {
    const reconciler = new StreamReconciler({backfillChunk: 400});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 300}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    // A live batch re-declaring floor zero says the log now streaming is
    // complete, so there is no older range left to wait for.
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0}), FRESH);
    expect(reconciler.beginBackfill()).toEqual({kind: 'complete'});
    // The outstanding range still settles: the caller owes it an answer.
    expect(reconciler.settleBackfill(request, [event(150)])).toEqual({
      kind: 'prepend',
      events: [event(150)],
      historyFloor: 0,
    });
  });

  it('filters spine events the tail already delivered out of a chunk', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const {message, spine} = bootstrap('log', 200, [201, 202]);
    reconciler.reconcileBatch(message, FRESH);
    const chunk = [101, ...spine.filter(s => s >= 101), 150, 199].map(s => event(s, SPINE_TYPE));
    const settled = prepended(
      reconciler.settleBackfill(fetched(reconciler.beginBackfill()), chunk),
    );
    expect(sequences(settled.events)).toEqual([101, 150, 199]);
    expect(settled.historyFloor).toBe(100);
  });

  it('never filters an unsequenced event', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const message = batch({storeId: 'log', declaredFloor: 200, sequences: [undefined, 7]});
    reconciler.reconcileBatch(message, FRESH);
    const settled = reconciler.settleBackfill(fetched(reconciler.beginBackfill()), [
      event(undefined),
      event(undefined),
      event(7),
    ]);
    expect(sequences(prepended(settled).events)).toEqual([undefined, undefined]);
  });

  it('hands back an empty chunk as an empty prepend that still lowers the floor', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 200}), FRESH);
    expect(reconciler.settleBackfill(fetched(reconciler.beginBackfill()), [])).toEqual({
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
   * chunk the caller hands back, which is what this pins, together with the
   * batch not disturbing a spine an earlier bootstrap recorded. Sequence 0 is
   * protocol-legal: `RunEvent.sequence` carries no lower bound
   * (`src/server/api/protocol.py:247`).
   */
  it('records no spine and disturbs none for a batch that declares floor zero', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const {message} = bootstrap('log', 200, [201]);
    reconciler.reconcileBatch(message, FRESH);
    const request = fetched(reconciler.beginBackfill());
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0, sequences: [0]}), FRESH),
    ).toEqual({kind: 'extend', historyFloor: 0});
    const settled = reconciler.settleBackfill(request, [
      event(0),
      event(120, SPINE_TYPE),
      event(150),
    ]);
    expect(sequences(prepended(settled).events)).toEqual([0, 150]);
  });

  it('rejects a response wholesale when a re-bootstrap landed while it was in flight', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 200}), FRESH);
    const request = fetched(reconciler.beginBackfill());
    reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 900}), FRESH);
    expect(reconciler.settleBackfill(request, [event(150), event(199)])).toEqual({
      kind: 'superseded',
    });
  });

  it('leaves the floor where the re-bootstrap put it when it rejects a superseded response', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 200}), FRESH);
    const stale = fetched(reconciler.beginBackfill());
    reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 900}), FRESH);
    reconciler.settleBackfill(stale, [event(150)]);
    // The next ask backfills against the new log's own numbering, from 900.
    const next = fetched(reconciler.beginBackfill());
    expect(next.afterSequence).toBe(800);
    expect(prepended(reconciler.settleBackfill(next, [])).historyFloor).toBe(800);
  });

  it('clears the spine set on a re-bootstrap but not on a backfill descent', () => {
    const chunk = [event(1, SPINE_TYPE), event(50)];
    const {message} = bootstrap('log', 200, [201]);

    const descent = new StreamReconciler({backfillChunk: 100});
    descent.reconcileBatch(message, FRESH);
    descent.settleBackfill(fetched(descent.beginBackfill()), []);
    // The floor is 100 now; the spine recorded at 200 must still filter.
    const kept = descent.settleBackfill(fetched(descent.beginBackfill()), chunk);
    expect(sequences(prepended(kept).events)).toEqual([50]);

    const reset = new StreamReconciler({backfillChunk: 100});
    reset.reconcileBatch(message, FRESH);
    reset.reconcileBatch(batch({storeId: 'log', declaredFloor: 800}), FRESH);
    const unfiltered = reset.settleBackfill(fetched(reset.beginBackfill()), chunk);
    expect(sequences(prepended(unfiltered).events)).toEqual([1, 50]);
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
  | {readonly op: 'abandon'; readonly id: number};

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
  /** The floor every batch on the live connection declares. */
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
 * (`subscription_bootstrap`, `src/server/api/service.py:353-386`).
 */
function bootstrapDial(rng: Rng, tops: Map<string, number>, stream: Stream): Command {
  if (stream.reportsIdentity && rng.chance(0.3)) stream.log = rng.pick(LOGS);
  stream.floor = Math.max(0, logTop(rng, tops, stream.log) - TAIL);
  stream.resumed = false;
  stream.bootstrapped = true;
  return batchOn(rng, tops, stream, spineOf(stream.floor));
}

/**
 * A resume dial. It carries the client's cursor and no tail, so the server
 * declares the cursor as the floor and replays no spine. A cursor naming a
 * store the journal has since replaced is dropped, and the live log is
 * replayed whole from zero (`src/server/api/service.py:372-375`).
 */
function resumeDial(rng: Rng, tops: Map<string, number>, stream: Stream): Command {
  const swapped = stream.reportsIdentity && rng.chance(0.3);
  if (swapped) stream.log = rng.pick(LOGS.filter(log => log !== stream.log));
  stream.floor = swapped ? 0 : logTop(rng, tops, stream.log);
  stream.resumed = true;
  return batchOn(rng, tops, stream, swapped ? span(1, logTop(rng, tops, stream.log)) : []);
}

/**
 * One backfill response, dense over the range the request asked for. Both
 * bounds are exclusive (`src/server/api/protocol.py:183-187`), so a chunk
 * covering a bootstrap floor redelivers the spine event sitting at it, which
 * is what the reconciler has to filter.
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
 * against one that does not, and backfills that settle out of order, are
 * abandoned, or are asked for again while one is outstanding.
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
      commands.push(rng.chance(0.15) ? {op: 'abandon', id} : {op: 'settle', id, seed});
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
      readonly kind: BackfillPlan['kind'];
      readonly range: readonly [number, number] | null;
    }
  | {
      readonly op: 'settle';
      readonly kind: BackfillReconciliation['kind'];
      readonly events: readonly (number | undefined)[];
      readonly historyFloor: number | null;
    }
  | {readonly op: 'abandon'};

/**
 * The reconciler's surface, so the differential oracle can be driven by the
 * same harness without either side knowing about the other.
 */
interface Reconciling {
  storeId(): string;
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation;
  beginBackfill(): BackfillPlan;
  settleBackfill(request: BackfillRequest, events: readonly RunEvent[]): BackfillReconciliation;
  abandonBackfill(request: BackfillRequest): void;
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
  readonly historyFloor: number;
  readonly storeId: string;
}

/** The walk's mutable position, threaded through the per-command handlers. */
interface Walk {
  readonly reconciler: Reconciling;
  readonly requests: Map<number, BackfillRequest | null>;
  readonly folded: Set<number>;
  readonly refolded: number[];
  readonly observations: Observation[];
  historyFloor: number;
  prepends: number;
  filtered: number;
  record: boolean;
}

function walkBegin(walk: Walk, command: Extract<Command, {op: 'begin'}>): void {
  const plan = walk.reconciler.beginBackfill();
  walk.requests.set(command.id, plan.kind === 'fetch' ? plan.request : null);
  if (!walk.record) return;
  walk.observations.push({
    op: 'begin',
    kind: plan.kind,
    range: plan.kind === 'fetch' ? [plan.request.afterSequence, plan.request.beforeSequence] : null,
  });
}

function walkSettle(walk: Walk, command: Extract<Command, {op: 'settle'}>): void {
  const request = walk.requests.get(command.id);
  if (request === undefined || request === null) return;
  const chunk = chunkForRange(request, command.seed);
  const settled = walk.reconciler.settleBackfill(request, chunk);
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

function walkAbandon(walk: Walk, command: Extract<Command, {op: 'abandon'}>): void {
  const request = walk.requests.get(command.id);
  if (request === undefined || request === null) return;
  walk.reconciler.abandonBackfill(request);
  if (walk.record) walk.observations.push({op: 'abandon'});
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
 * each disposition reports, and keeps the requests it has not settled yet.
 * Observations are recorded from `recordFrom` onwards.
 */
function drive(reconciler: Reconciling, commands: readonly Command[], recordFrom = 0): Trace {
  const walk: Walk = {
    reconciler,
    requests: new Map(),
    folded: new Set(),
    refolded: [],
    observations: [],
    historyFloor: 0,
    prepends: 0,
    filtered: 0,
    record: recordFrom === 0,
  };
  commands.forEach((command, index) => {
    walk.record = index >= recordFrom;
    if (index === recordFrom) {
      // The counters describe the recorded window, not the warm-up before it.
      walk.refolded.length = 0;
      walk.prepends = 0;
      walk.filtered = 0;
    }
    if (command.op === 'begin') walkBegin(walk, command);
    else if (command.op === 'settle') walkSettle(walk, command);
    else if (command.op === 'abandon') walkAbandon(walk, command);
    else walkBatch(walk, command);
  });
  return {
    observations: walk.observations,
    folded: [...walk.folded].sort((left, right) => left - right),
    refolded: walk.refolded,
    prepends: walk.prepends,
    filtered: walk.filtered,
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

describe('StreamReconciler properties', () => {
  for (const seed of SEEDS) {
    it(`only ever raises the floor through a re-bootstrap (seed ${seed})`, () => {
      const trace = drive(
        new StreamReconciler({backfillChunk: CHUNK}),
        generateCommands(new Rng(seed), 80),
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

    it(`asks only for ranges inside the log, sized by the chunk (seed ${seed})`, () => {
      const {observations} = drive(
        new StreamReconciler({backfillChunk: CHUNK}),
        generateCommands(new Rng(seed), 80),
      );
      let fetches = 0;
      for (const observation of observations) {
        if (observation.op !== 'begin' || observation.range === null) continue;
        fetches += 1;
        const [after, before] = observation.range;
        expect(after).toBeGreaterThanOrEqual(0);
        // Both bounds exclusive, so the range spans `before - after - 1`
        // sequences, capped by the start of the log.
        expect(before - after - 1).toBeLessThanOrEqual(CHUNK);
        expect(before - after - 1).toBeGreaterThan(0);
      }
      expect(fetches).toBeGreaterThan(0);
    });

    it(`never hands back a backfilled event the caller already folded (seed ${seed})`, () => {
      // `drive` applies every disposition to the set standing in for the folded
      // log and records any sequence a prepend handed back twice, which is what
      // folding a spine event a second time would look like.
      const trace = drive(
        new StreamReconciler({backfillChunk: CHUNK}),
        generateCommands(new Rng(seed), 80),
      );
      expect(trace.refolded).toEqual([]);
      // The spine set only ever holds sequences the caller folded from the same
      // batch, and a re-bootstrap clears both together, so every event the
      // filter dropped is one the assertion above would have caught. A positive
      // count is therefore exactly the claim that the property is not vacuous.
      expect(trace.filtered).toBeGreaterThan(0);
      expect(trace.prepends).toBeGreaterThan(0);
    });

    /**
     * With nothing in flight. A range outstanding across the re-bootstrap
     * deliberately survives it (the TUI's `#historyFetch` does too), so the
     * prefix is drained first: abandoning an id that is not outstanding is a
     * no-op, so one abandon per possible id clears whatever it left.
     */
    it(`makes a re-bootstrap independent of everything before it (seed ${seed})`, () => {
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
      const trails = [20, 45].map(length => {
        const prefix = generateCommands(new Rng(seed + length), length);
        const drain: readonly Command[] = span(0, prefix.length).map(id => ({op: 'abandon', id}));
        const commands = [...prefix, ...drain, seeded, cut, ...after];
        return drive(
          new StreamReconciler({backfillChunk: CHUNK}),
          commands,
          prefix.length + drain.length + 1,
        );
      });
      expect(trails[0]?.observations[0]).toEqual({
        op: 'batch',
        decision: {kind: 'rebootstrap', historyFloor: 640},
      });
      expect(trails[1]).toEqual(trails[0] as Trace);
    });

    it(`treats a batch repeating what the stream already said as a no-op (seed ${seed})`, () => {
      const reconciler = new StreamReconciler({backfillChunk: CHUNK});
      const trace = drive(reconciler, generateCommands(new Rng(seed), 50));
      // The floor the caller holds is at or below the floor the stream last
      // declared, so a batch re-declaring it is neither a raise nor a descent,
      // on either path.
      const floor = trace.historyFloor;
      const repeat = batch({storeId: trace.storeId, declaredFloor: floor});
      expect(reconciler.reconcileBatch(repeat, FRESH)).toEqual({
        kind: 'extend',
        historyFloor: floor,
      });
      expect(reconciler.reconcileBatch(repeat, RESUMED)).toEqual({
        kind: 'extend',
        historyFloor: floor,
      });
    });
  }
});

/**
 * The TUI controller's reconciliation, transcribed from
 * `clients/tui/src/session-controller.ts` at merge base 667a08f8.
 *
 * Kept in the controller's own shape (a resumed and a fresh batch method, the
 * floor and spine helpers, the single-flight gate, and the floor read back out
 * of the state the controller wrote) rather than the reconciler's. It is a
 * transliteration, not an independent derivation, so it does not confirm the
 * extraction is right; what it does is fail loudly if the controller and the
 * reconciler drift before phase (b) migrates the controller onto it. The
 * equivalence argument is the hand re-derivation recorded in the PR body.
 *
 * Line references in that file:
 *
 * - fields: 299 `#foldedBelowFloor`, 301 `#historyFloor`, 310 `#declaredFloor`,
 *   319 `#storeId`, 328 `#rebootstrapGeneration`, and `#historyFetch`
 * - `storeId` subscription callback: 381
 * - `loadOlderHistory`: 425-433, `#requestOlderHistory`: 435-465
 * - `#reconcileBatch`: 1419-1425, `#reconcileResumedBatch`: 1427-1460,
 *   `#reconcileFreshBatch`: 1462-1483
 * - `#lowerHistoryFloor`: 1493-1496, `#resetHistoryFloor`: 1509-1514,
 *   `#recordSpine`: 1517-1525
 *
 * Two things are deliberately not transcribed, because they are the
 * differences this phase introduces rather than drift:
 *
 * - the declared-floor validation, which the controller does not do. The
 *   generator only produces floors a conforming server can send, so the
 *   validation never fires inside the differential runs.
 * - promise sharing. `loadOlderHistory` hands a second caller the first
 *   caller's promise; `beginBackfill` reports `in-flight` and leaves the
 *   sharing to the caller, which is where the request lives. The oracle
 *   models the gate, not the promise.
 */
class ControllerOracle implements Reconciling {
  readonly #backfillChunk: number;
  readonly #foldedBelowFloor = new Set<number>();
  #historyFloor = Number.POSITIVE_INFINITY;
  #declaredFloor: number | null = null;
  #storeId: string | null = null;
  #rebootstrapGeneration = 0;
  /** `#historyFetch`: the controller holds a promise, the oracle its identity. */
  #fetch: {readonly stamp: number; readonly generation: number; readonly nextFloor: number} | null =
    null;
  #issued = 0;
  /**
   * `this.#state.core.historyAfterSequence`. The controller reads the floor
   * back out of the state it wrote, which is the second source of truth the
   * reconciler replaces with its own field. Keeping it here is what makes the
   * differential test check that the two agree at every step.
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

  // Lines 425-433 and 436-438.
  beginBackfill(): BackfillPlan {
    if (this.#stateFloor === 0) return {kind: 'complete'};
    if (this.#fetch !== null) return {kind: 'in-flight'};
    const nextFloor = Math.max(0, this.#stateFloor - this.#backfillChunk);
    this.#issued += 1;
    this.#fetch = {stamp: this.#issued, generation: this.#rebootstrapGeneration, nextFloor};
    return {
      kind: 'fetch',
      request: {
        afterSequence: nextFloor,
        beforeSequence: this.#stateFloor + 1,
        stamp: this.#issued as BackfillStamp,
      },
    };
  }

  // Lines 439-458, with the `finally` at 428-430.
  settleBackfill(request: BackfillRequest, events: readonly RunEvent[]): BackfillReconciliation {
    const fetch = this.#fetch;
    if (fetch === null || fetch.stamp !== request.stamp) return {kind: 'superseded'};
    this.#fetch = null;
    if (this.#rebootstrapGeneration !== fetch.generation) return {kind: 'superseded'};
    const filtered = events.filter(
      item => item.sequence === undefined || !this.#foldedBelowFloor.has(item.sequence),
    );
    this.#stateFloor = this.#lowerHistoryFloor(fetch.nextFloor);
    return {kind: 'prepend', events: filtered, historyFloor: this.#stateFloor};
  }

  // Lines 459-464: the floor stays where it was, so the same range is retried.
  abandonBackfill(request: BackfillRequest): void {
    if (this.#fetch !== null && this.#fetch.stamp === request.stamp) this.#fetch = null;
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
    it(`decides every command exactly as the controller does (seed ${seed})`, () => {
      const commands = generateCommands(new Rng(seed), 150);
      expect(drive(new StreamReconciler({backfillChunk: CHUNK}), commands)).toEqual(
        drive(new ControllerOracle(CHUNK), commands),
      );
    });
  }

  it('agrees across the whole store identity and floor matrix', () => {
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
    let compared = 0;
    for (const resumed of [false, true]) {
      for (const first of payloads) {
        for (const second of payloads) {
          const commands: readonly Command[] = [
            {op: 'batch', resumed: false, message: first},
            {op: 'begin', id: 0},
            {op: 'batch', resumed, message: second},
            {op: 'settle', id: 0, seed: 17},
            {op: 'begin', id: 1},
            {op: 'abandon', id: 1},
            {op: 'begin', id: 2},
            {op: 'settle', id: 2, seed: 23},
            probe,
            {op: 'begin', id: 3},
            {op: 'settle', id: 3, seed: 29},
          ];
          expect(drive(new StreamReconciler({backfillChunk: 60}), commands)).toEqual(
            drive(new ControllerOracle(60), commands),
          );
          compared += 1;
        }
      }
    }
    expect(compared).toBe(2 * payloads.length * payloads.length);
  });
});
