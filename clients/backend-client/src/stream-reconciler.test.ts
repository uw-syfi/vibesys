import {describe, expect, it} from 'bun:test';
import type {EventBatchMessage, RunEvent} from './protocol.js';
import {
  type BackfillReconciliation,
  type BackfillRequest,
  type BatchContext,
  type BatchReconciliation,
  StreamReconciler,
} from './stream-reconciler.js';

const SPINE_TYPE = 'round_finished';
const TAIL_TYPE = 'agent_output_chunk';

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

/**
 * A payload whose optional keys carry an explicit `null`.
 *
 * The generated type says `store_id` and `history_after_sequence` are an
 * optional string and number, but JSON can carry `null` in either, and the
 * reconciler reads both through `??`. Building that payload needs the cast;
 * the point of the tests using it is that `null` and an omitted key are the
 * same input.
 */
function withNulls(message: EventBatchMessage): EventBatchMessage {
  return {...message, store_id: null, history_after_sequence: null} as unknown as EventBatchMessage;
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

function ranged(request: BackfillRequest | null): BackfillRequest {
  if (request === null) throw new Error('expected a backfill range, got history complete');
  return request;
}

/** A bootstrap batch: the run-level spine from below `floor`, then the tail above it. */
function bootstrap(
  storeId: string | undefined,
  floor: number,
  tail: readonly number[],
): {readonly message: EventBatchMessage; readonly spine: readonly number[]} {
  const spine = floor === 0 ? [] : [...new Set([1, Math.max(1, floor >> 1), floor])];
  const message: EventBatchMessage = {
    type: 'event_batch',
    events: [...spine.map(s => event(s, SPINE_TYPE)), ...tail.map(s => event(s))],
    history_after_sequence: floor,
  };
  if (storeId !== undefined) message.store_id = storeId;
  return {message, spine};
}

const fresh = (historyFloor: number): BatchContext => ({resumed: false, historyFloor});
const resume = (historyFloor: number): BatchContext => ({resumed: true, historyFloor});

describe('StreamReconciler batch dispositions', () => {
  it('never re-bootstraps the first batch, whatever floor and store it declares', () => {
    const specs: readonly BatchSpec[] = [
      {},
      {storeId: 'log-a', declaredFloor: 900},
      {declaredFloor: 5_000},
      {storeId: ''},
    ];
    for (const spec of specs) {
      const reconciler = new StreamReconciler();
      expect(reconciler.reconcileBatch(batch(spec), fresh(0))).toEqual({
        kind: 'extend',
        historyFloor: spec.declaredFloor ?? 0,
      });
    }
  });

  it('re-bootstraps a fresh batch that names a different store', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 40}), fresh(0));
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 40}), fresh(40)),
    ).toEqual({kind: 'rebootstrap', historyFloor: 40});
    expect(reconciler.storeId()).toBe('run-log');
  });

  it('re-bootstraps a fresh batch declaring more history than the stream has declared', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), fresh(0));
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), fresh(100)),
    ).toEqual({kind: 'rebootstrap', historyFloor: 900});
  });

  it('extends a fresh batch that re-declares the floor the stream already declared', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), fresh(0));
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), fresh(100)),
    ).toEqual({kind: 'extend', historyFloor: 100});
  });

  it('keeps a backfilled floor when a later live batch re-declares the bootstrap floor', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), fresh(0));
    const settled = reconciler.settleBackfill(ranged(reconciler.beginBackfill(1_500)), []);
    expect(settled).toEqual({kind: 'prepend', events: [], historyFloor: 500});

    // The subscription re-declares its own bootstrap floor on every live batch.
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), fresh(500)),
    ).toEqual({kind: 'extend', historyFloor: 500});
  });

  it('takes a re-bootstrapped floor literally when it rises', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 0}), fresh(0));
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 4_200}), fresh(0)),
    ).toEqual({kind: 'rebootstrap', historyFloor: 4_200});
  });

  it('takes a re-bootstrapped floor literally when a shorter run log replays whole', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 2_000}), fresh(0));
    // A run log shorter than the tail is replayed whole and declares floor 0.
    // That is the truth about the log now streaming, not a backfill descent.
    expect(
      reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 0}), fresh(2_000)),
    ).toEqual({kind: 'rebootstrap', historyFloor: 0});
    expect(reconciler.beginBackfill(0)).toBeNull();
  });

  /**
   * `PersistentEventStream` resumes only from a cursor a bootstrap batch
   * established, so this ordering does not occur on a live stream. It is
   * pinned because it is the one case where the caller's floor and the floor
   * the stream has declared disagree: a resume before any bootstrap declares
   * nothing, so the next fresh batch is still a first batch and takes its own
   * floor literally rather than as a descent.
   */
  it('lets a resume before any bootstrap declare no floor at all', () => {
    const reconciler = new StreamReconciler();
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), resume(0)),
    ).toEqual({kind: 'extend', historyFloor: 0});
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 900}), fresh(0)),
    ).toEqual({kind: 'extend', historyFloor: 900});
  });

  it('keeps the caller floor on a resume so scrollback survives a reconnect', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), fresh(0));
    // The resume re-declares the subscription's own bootstrap floor, which is
    // above what the reader has already backfilled down to.
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 1_500}), resume(200)),
    ).toEqual({kind: 'extend', historyFloor: 200});
  });

  it('treats a resumed batch from a different store as a fresh bootstrap, spine included', () => {
    const reconciler = new StreamReconciler();
    const before = bootstrap('server-log', 100, [101, 102]);
    reconciler.reconcileBatch(before.message, fresh(0));
    const swapped = bootstrap('run-log', 60, [61]);
    expect(reconciler.reconcileBatch(swapped.message, resume(100))).toEqual({
      kind: 'rebootstrap',
      historyFloor: 60,
    });
    expect(reconciler.storeId()).toBe('run-log');
    // The new store's spine is tracked, and the superseded store's is gone.
    const settled = reconciler.settleBackfill(
      ranged(reconciler.beginBackfill(60)),
      [...swapped.spine, ...before.spine].map(sequence => event(sequence, SPINE_TYPE)),
    );
    expect(sequences(prepended(settled).events)).toEqual(
      before.spine.filter(sequence => !swapped.spine.includes(sequence)),
    );
  });
});

describe('StreamReconciler store identity', () => {
  it('reports an empty store id until a batch names one', () => {
    const reconciler = new StreamReconciler();
    expect(reconciler.storeId()).toBe('');
    reconciler.reconcileBatch(batch({declaredFloor: 10}), fresh(0));
    expect(reconciler.storeId()).toBe('');
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), fresh(10));
    expect(reconciler.storeId()).toBe('log');
  });

  it('reads an explicit null store id and floor exactly as omitted keys', () => {
    const omitted = new StreamReconciler();
    const nulled = new StreamReconciler();
    expect(nulled.reconcileBatch(withNulls(batch({sequences: [1, 2]})), fresh(0))).toEqual(
      omitted.reconcileBatch(batch({sequences: [1, 2]}), fresh(0)),
    );
    expect(nulled.storeId()).toBe(omitted.storeId());
  });

  it('leaves the declared floor as the only re-bootstrap signal with no reported identity', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({declaredFloor: 100}), fresh(0));
    // The same (empty) identity on both sides, so only a raised floor can say so.
    expect(reconciler.reconcileBatch(batch({declaredFloor: 100}), fresh(100))).toEqual({
      kind: 'extend',
      historyFloor: 100,
    });
    expect(reconciler.reconcileBatch(batch({declaredFloor: 700}), fresh(100))).toEqual({
      kind: 'rebootstrap',
      historyFloor: 700,
    });
  });

  it('compares an empty declared store against an empty known store on the fresh path', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: '', declaredFloor: 10}), fresh(0));
    expect(reconciler.reconcileBatch(batch({storeId: '', declaredFloor: 10}), fresh(10))).toEqual({
      kind: 'extend',
      historyFloor: 10,
    });
  });

  it('keeps the last named store across a resume that names none', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 10}), fresh(0));
    expect(reconciler.reconcileBatch(batch({declaredFloor: 10}), resume(10))).toEqual({
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
    const onFresh = new StreamReconciler();
    const onResume = new StreamReconciler();
    const named = batch({storeId: 'log', declaredFloor: 10});
    const anonymous = batch({declaredFloor: 10});
    onFresh.reconcileBatch(named, fresh(0));
    onResume.reconcileBatch(named, fresh(0));

    expect(onFresh.reconcileBatch(anonymous, fresh(10))).toEqual({
      kind: 'rebootstrap',
      historyFloor: 10,
    });
    expect(onResume.reconcileBatch(anonymous, resume(10))).toEqual({
      kind: 'extend',
      historyFloor: 10,
    });
    expect(onFresh.storeId()).toBe('');
    expect(onResume.storeId()).toBe('log');
  });

  /**
   * Also pinned as current behavior. `#declaredFloor` holds the floor the last
   * batch declared, not the highest one the stream ever declared, so a
   * store-preserving descent is taken as a backfill descent (no discard, no
   * spine clear, no generation bump) and the next batch back at the original
   * floor re-bootstraps instead. Reported as a candidate follow-up.
   */
  it('takes a store-preserving floor descent as a backfill descent, then re-bootstraps on the way back up (current behavior)', () => {
    const reconciler = new StreamReconciler();
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), fresh(0));
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 50}), fresh(100)),
    ).toEqual({kind: 'extend', historyFloor: 50});
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 100}), fresh(50)),
    ).toEqual({kind: 'rebootstrap', historyFloor: 100});
  });
});

describe('StreamReconciler backfill', () => {
  it('reports history complete at floor zero', () => {
    expect(new StreamReconciler().beginBackfill(0)).toBeNull();
  });

  it('asks for the chunk below the floor, including the floor itself', () => {
    const reconciler = new StreamReconciler({backfillChunk: 40});
    expect(reconciler.beginBackfill(100)).toEqual({
      afterSequence: 60,
      beforeSequence: 101,
      generation: 0,
    });
    // The range never reaches below the start of the log.
    expect(reconciler.beginBackfill(10)).toEqual({
      afterSequence: 0,
      beforeSequence: 11,
      generation: 0,
    });
  });

  it('defaults the chunk to the bootstrap tail', () => {
    expect(new StreamReconciler().beginBackfill(5_000)?.afterSequence).toBe(4_000);
  });

  it('filters spine events the tail already delivered out of a chunk', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const {message, spine} = bootstrap('log', 200, [201, 202]);
    reconciler.reconcileBatch(message, fresh(0));
    const chunk = [101, ...spine.filter(s => s >= 101), 150, 199].map(s => event(s, SPINE_TYPE));
    const settled = prepended(
      reconciler.settleBackfill(ranged(reconciler.beginBackfill(200)), chunk),
    );
    expect(sequences(settled.events)).toEqual([101, 150, 199]);
    expect(settled.historyFloor).toBe(100);
  });

  it('never filters an unsequenced event', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const message = batch({storeId: 'log', declaredFloor: 200, sequences: [undefined, 7]});
    reconciler.reconcileBatch(message, fresh(0));
    const settled = reconciler.settleBackfill(ranged(reconciler.beginBackfill(200)), [
      event(undefined),
      event(undefined),
      event(7),
    ]);
    expect(sequences(prepended(settled).events)).toEqual([undefined, undefined]);
  });

  /**
   * `#recordSpine` short-circuits on `history_after_sequence === 0`. The
   * short-circuit is defensive rather than load-bearing: at floor 0 the only
   * sequences it could record are `<= 0`, which no log numbers, and floor 0
   * means history is complete so there is no range left to ask for. What is
   * observable through this interface is that such a batch adds nothing and
   * disturbs nothing, so a request taken before it still filters the spine the
   * earlier bootstrap recorded.
   */
  it('records no spine and disturbs none for a batch that declares floor zero', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    const {message} = bootstrap('log', 200, [201]);
    reconciler.reconcileBatch(message, fresh(0));
    const request = ranged(reconciler.beginBackfill(200));
    expect(
      reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 0}), fresh(200)),
    ).toEqual({kind: 'extend', historyFloor: 0});
    const settled = reconciler.settleBackfill(request, [event(100, SPINE_TYPE), event(150)]);
    expect(sequences(prepended(settled).events)).toEqual([150]);
  });

  it('accepts an absent events field as an empty chunk', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 200}), fresh(0));
    expect(reconciler.settleBackfill(ranged(reconciler.beginBackfill(200)), undefined)).toEqual({
      kind: 'prepend',
      events: [],
      historyFloor: 100,
    });
  });

  it('rejects a response wholesale when a re-bootstrap landed while it was in flight', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 200}), fresh(0));
    const request = ranged(reconciler.beginBackfill(200));
    reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 900}), fresh(200));
    expect(reconciler.settleBackfill(request, [event(150), event(199)])).toEqual({
      kind: 'superseded',
    });
  });

  it('leaves the floor where the re-bootstrap put it when it rejects a superseded response', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'server-log', declaredFloor: 200}), fresh(0));
    const stale = ranged(reconciler.beginBackfill(200));
    reconciler.reconcileBatch(batch({storeId: 'run-log', declaredFloor: 900}), fresh(200));
    reconciler.settleBackfill(stale, [event(150)]);
    // The next ask backfills against the new log's own numbering, from 900.
    const next = ranged(reconciler.beginBackfill(900));
    expect(next.afterSequence).toBe(800);
    expect(prepended(reconciler.settleBackfill(next, [])).historyFloor).toBe(800);
  });

  /**
   * The documented precondition on `beginBackfill`: the floor only moves when
   * a response is folded, so two ranges taken against the same floor are the
   * same range and settle to the same events. The caller single-flights (the
   * TUI through its `#historyFetch` promise); the reconciler cannot, because
   * it has no handle on the request in flight.
   */
  it('answers two ranges taken against one floor identically, so the caller must single-flight', () => {
    const reconciler = new StreamReconciler({backfillChunk: 100});
    reconciler.reconcileBatch(batch({storeId: 'log', declaredFloor: 200}), fresh(0));
    const first = ranged(reconciler.beginBackfill(200));
    const second = ranged(reconciler.beginBackfill(200));
    expect(second).toEqual(first);
    const chunk = [event(150)];
    expect(sequences(prepended(reconciler.settleBackfill(first, chunk)).events)).toEqual([150]);
    expect(sequences(prepended(reconciler.settleBackfill(second, chunk)).events)).toEqual([150]);
  });

  it('clears the spine set on a re-bootstrap but not on a backfill descent', () => {
    const chunk = [event(1, SPINE_TYPE), event(50)];
    const {message} = bootstrap('log', 200, [201]);

    const descent = new StreamReconciler({backfillChunk: 100});
    descent.reconcileBatch(message, fresh(0));
    descent.settleBackfill(ranged(descent.beginBackfill(200)), []);
    // The floor is 100 now; the spine recorded at 200 must still filter.
    const kept = descent.settleBackfill(ranged(descent.beginBackfill(100)), chunk);
    expect(sequences(prepended(kept).events)).toEqual([50]);

    const reset = new StreamReconciler({backfillChunk: 100});
    reset.reconcileBatch(message, fresh(0));
    reset.reconcileBatch(batch({storeId: 'log', declaredFloor: 800}), fresh(200));
    const unfiltered = reset.settleBackfill(ranged(reset.beginBackfill(800)), chunk);
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

/** One command a client issues against the reconciler. */
type Command =
  | {readonly op: 'batch'; readonly resumed: boolean; readonly message: EventBatchMessage}
  | {readonly op: 'begin'; readonly id: number}
  | {readonly op: 'settle'; readonly id: number; readonly events: readonly RunEvent[]};

interface GeneratorState {
  /** The log now streaming. Each log numbers its own sequences from 1. */
  log: string;
  /** Whether the server reports that log's identity, which an old server does not. */
  named: boolean;
  /** The floor the live subscription declares on every batch it sends. */
  floor: number;
  /**
   * Whether a bootstrap batch has landed. `PersistentEventStream` resumes only
   * from a cursor a bootstrap batch established, so the first batch of a
   * stream is always a fresh one.
   */
  bootstrapped: boolean;
}

/** Where a log currently ends. A log the client has not streamed yet has history already. */
function logTop(rng: Rng, tops: Map<string, number>, log: string): number {
  const existing = tops.get(log);
  if (existing !== undefined) return existing;
  const top = rng.int(200, 3_000);
  tops.set(log, top);
  return top;
}

/** Points the stream at a log and a bootstrap floor, as a fresh subscription does. */
function reseat(rng: Rng, tops: Map<string, number>, state: GeneratorState): void {
  state.log = rng.pick(LOGS);
  state.named = rng.chance(0.8);
  const top = logTop(rng, tops, state.log);
  // The subscription bootstraps against a tail of the log, or replays it whole.
  state.floor = rng.chance(0.25) ? 0 : Math.max(0, top - rng.int(1, 1_200));
}

function nextSequences(rng: Rng, tops: Map<string, number>, log: string): readonly number[] {
  const tail: number[] = [];
  for (let index = 0; index < rng.int(0, 4); index += 1) {
    const next = logTop(rng, tops, log) + 1;
    tops.set(log, next);
    tail.push(next);
  }
  return tail;
}

function batchCommand(rng: Rng, tops: Map<string, number>, state: GeneratorState): Command {
  const swapped = rng.chance(0.25);
  if (swapped) reseat(rng, tops, state);
  const resumed = state.bootstrapped && !swapped && rng.chance(0.4);
  state.bootstrapped = true;
  // A bootstrap replays the run-level spine from below its floor; a resume
  // picks up after the caller's cursor, which is above the floor already.
  const spine =
    resumed || state.floor === 0
      ? []
      : [...new Set([1, Math.max(1, state.floor >> 1), state.floor])];
  const message: EventBatchMessage = {
    type: 'event_batch',
    events: [
      ...spine.map(sequence => event(sequence, SPINE_TYPE)),
      ...nextSequences(rng, tops, state.log).map(sequence => event(sequence)),
    ],
    history_after_sequence: state.floor,
  };
  if (state.named) message.store_id = state.log;
  return {op: 'batch', resumed, message};
}

/**
 * One backfill response, drawn from the band a request around `floor` covers.
 * Sequences are distinct: a `query.events` range answers with each event once.
 */
function chunkEvents(rng: Rng, floor: number): readonly RunEvent[] {
  const drawn = new Set<number>();
  const events: RunEvent[] = [];
  for (let item = 0; item < rng.int(0, 5); item += 1) {
    if (rng.chance(0.1)) {
      events.push(event(undefined, SPINE_TYPE));
      continue;
    }
    const sequence = rng.int(Math.max(0, floor - 1_600), floor + 1);
    if (drawn.has(sequence)) continue;
    drawn.add(sequence);
    events.push(event(sequence, SPINE_TYPE));
  }
  return events.sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0));
}

/**
 * A random command sequence shaped like real traffic: bootstrap batches that
 * replay the spine below their floor, live batches that re-declare the same
 * floor, resumes, log swaps with and without reported identity, and backfills
 * whose responses settle out of order or long after they were asked for.
 * Sequences are monotone within a log, as a real store's are.
 */
function generateCommands(rng: Rng, length: number): readonly Command[] {
  const commands: Command[] = [];
  const tops = new Map<string, number>();
  const open: number[] = [];
  const state: GeneratorState = {log: LOGS[0], named: true, floor: 0, bootstrapped: false};
  reseat(rng, tops, state);
  let issued = 0;
  for (let index = 0; index < length; index += 1) {
    if (open.length > 0 && rng.chance(0.35)) {
      const id = open.splice(rng.int(0, open.length - 1), 1)[0] as number;
      commands.push({op: 'settle', id, events: chunkEvents(rng, state.floor)});
      continue;
    }
    // One range at a time: the caller single-flights its backfill, so a second
    // request is never taken against a floor an answer has not yet moved.
    if (open.length === 0 && rng.chance(0.3)) {
      commands.push({op: 'begin', id: issued});
      open.push(issued);
      issued += 1;
      continue;
    }
    commands.push(batchCommand(rng, tops, state));
  }
  return commands;
}

/** What one command decided, as the caller can observe it. */
type Observation =
  | {readonly op: 'batch'; readonly decision: BatchReconciliation}
  | {readonly op: 'begin'; readonly range: readonly [number, number] | null}
  | {
      readonly op: 'settle';
      readonly kind: BackfillReconciliation['kind'];
      readonly events: readonly (number | undefined)[];
      readonly historyFloor: number | null;
    };

/**
 * The reconciler's surface, so the differential oracle can be driven by the
 * same harness without either side knowing about the other.
 */
interface Reconciling {
  storeId(): string;
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation;
  beginBackfill(historyFloor: number): BackfillRequest | null;
  settleBackfill(
    request: BackfillRequest,
    events: readonly RunEvent[] | undefined,
  ): BackfillReconciliation;
}

interface Trace {
  readonly observations: readonly Observation[];
  /** The sequences the caller ended up holding, standing in for its folded log. */
  readonly folded: readonly number[];
  /** Sequences a prepend handed back that the caller had already folded. */
  readonly refolded: readonly number[];
  readonly prepends: number;
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
  record: boolean;
}

function walkBegin(walk: Walk, command: Extract<Command, {op: 'begin'}>): void {
  const request = walk.reconciler.beginBackfill(walk.historyFloor);
  walk.requests.set(command.id, request);
  if (!walk.record) return;
  walk.observations.push({
    op: 'begin',
    range: request === null ? null : [request.afterSequence, request.beforeSequence],
  });
}

function walkSettle(walk: Walk, command: Extract<Command, {op: 'settle'}>): void {
  const request = walk.requests.get(command.id);
  if (request === undefined || request === null) return;
  // Only events inside the range the request asked for can come back.
  const inRange = command.events.filter(
    item =>
      item.sequence === undefined ||
      (item.sequence >= request.afterSequence && item.sequence < request.beforeSequence),
  );
  const settled = walk.reconciler.settleBackfill(request, inRange);
  if (settled.kind === 'prepend') {
    walk.prepends += 1;
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

function walkBatch(walk: Walk, command: Extract<Command, {op: 'batch'}>): void {
  const decision = walk.reconciler.reconcileBatch(command.message, {
    resumed: command.resumed,
    historyFloor: walk.historyFloor,
  });
  if (decision.kind === 'rebootstrap') walk.folded.clear();
  for (const item of command.message.events) {
    if (item.sequence !== undefined) walk.folded.add(item.sequence);
  }
  walk.historyFloor = decision.historyFloor;
  if (walk.record) walk.observations.push({op: 'batch', decision});
}

/**
 * Drives a reconciler the way a client does: it folds nothing, but it threads
 * the floor each decision returns back in as the floor it holds, applies each
 * disposition to a set standing in for the folded log, and keeps the requests
 * it has not settled yet. Observations are recorded from `recordFrom` onwards.
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
    record: recordFrom === 0,
  };
  commands.forEach((command, index) => {
    walk.record = index >= recordFrom;
    if (index === recordFrom) {
      // The counters describe the recorded window, not the warm-up before it.
      walk.refolded.length = 0;
      walk.prepends = 0;
    }
    if (command.op === 'begin') walkBegin(walk, command);
    else if (command.op === 'settle') walkSettle(walk, command);
    else walkBatch(walk, command);
  });
  return {
    observations: walk.observations,
    folded: [...walk.folded].sort((left, right) => left - right),
    refolded: walk.refolded,
    prepends: walk.prepends,
    storeId: reconciler.storeId(),
  };
}

function floorOf(observation: Observation): number | null {
  if (observation.op === 'batch') return observation.decision.historyFloor;
  if (observation.op === 'settle') return observation.historyFloor;
  return null;
}

describe('StreamReconciler properties', () => {
  for (const seed of SEEDS) {
    it(`only ever raises the floor through a re-bootstrap (seed ${seed})`, () => {
      const {observations} = drive(
        new StreamReconciler({backfillChunk: 400}),
        generateCommands(new Rng(seed), 80),
      );
      let floor: number | null = null;
      for (const observation of observations) {
        const next = floorOf(observation);
        if (next === null) continue;
        expect(next).toBeGreaterThanOrEqual(0);
        const raised = observation.op === 'batch' && observation.decision.kind === 'rebootstrap';
        if (floor !== null && !raised) expect(next).toBeLessThanOrEqual(floor);
        floor = next;
      }
    });

    it(`asks only for ranges inside the log, below the floor it holds (seed ${seed})`, () => {
      const {observations} = drive(
        new StreamReconciler({backfillChunk: 400}),
        generateCommands(new Rng(seed), 80),
      );
      for (const observation of observations) {
        if (observation.op !== 'begin' || observation.range === null) continue;
        const [after, before] = observation.range;
        expect(after).toBeGreaterThanOrEqual(0);
        expect(before).toBeGreaterThan(after);
      }
    });

    it(`never hands back a backfilled event the caller already folded (seed ${seed})`, () => {
      // `drive` applies every disposition to the set standing in for the
      // folded log and records any sequence a prepend handed back twice, which
      // is what folding a spine event a second time would look like.
      const trace = drive(
        new StreamReconciler({backfillChunk: 400}),
        generateCommands(new Rng(seed), 80),
      );
      expect(trace.refolded).toEqual([]);
      // The property is vacuous if nothing was ever prepended.
      expect(trace.prepends).toBeGreaterThan(0);
    });

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
        const commands = [...prefix, seeded, cut, ...after];
        return drive(new StreamReconciler({backfillChunk: 400}), commands, prefix.length + 1);
      });
      expect(trails[0]?.observations[0]).toEqual({
        op: 'batch',
        decision: {kind: 'rebootstrap', historyFloor: 640},
      });
      expect(trails[1]).toEqual(trails[0] as Trace);
    });

    it(`leaves the floor untouched when a batch repeats what the stream already said (seed ${seed})`, () => {
      const reconciler = new StreamReconciler({backfillChunk: 400});
      const {observations} = drive(reconciler, generateCommands(new Rng(seed), 50));
      const last = [...observations]
        .reverse()
        .find((observation): observation is Extract<Observation, {op: 'batch'}> => {
          return observation.op === 'batch';
        });
      if (last === undefined) return;
      // An empty batch that repeats the current store and floor is a no-op:
      // the floor it declares is the floor already held, on either path.
      const floor = last.decision.historyFloor;
      const repeat = batch({storeId: reconciler.storeId(), declaredFloor: floor});
      expect(reconciler.reconcileBatch(repeat, fresh(floor))).toEqual({
        kind: 'extend',
        historyFloor: floor,
      });
      expect(reconciler.reconcileBatch(repeat, resume(floor))).toEqual({
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
 * Kept in the controller's own shape (a resumed and a fresh batch method plus
 * the floor and spine helpers, each mutating then returning) rather than the
 * reconciler's, so the differential test compares two independently written
 * code paths. Line references in that file:
 *
 * - fields: 299 `#foldedBelowFloor`, 301 `#historyFloor`, 310 `#declaredFloor`,
 *   319 `#storeId`, 328 `#rebootstrapGeneration`
 * - `storeId` subscription callback: 381
 * - `loadOlderHistory` and `#requestOlderHistory`: 425-465
 * - `#reconcileBatch`: 1419-1425, `#reconcileResumedBatch`: 1427-1460,
 *   `#reconcileFreshBatch`: 1462-1483
 * - `#lowerHistoryFloor`: 1493-1496, `#resetHistoryFloor`: 1509-1514,
 *   `#recordSpine`: 1517-1525
 *
 * Re-check it against those lines if the controller changes before phase (b)
 * migrates it onto `StreamReconciler`.
 */
class ControllerOracle implements Reconciling {
  readonly #backfillChunk: number;
  readonly #foldedBelowFloor = new Set<number>();
  #historyFloor = Number.POSITIVE_INFINITY;
  #declaredFloor: number | null = null;
  #storeId: string | null = null;
  #rebootstrapGeneration = 0;

  constructor(backfillChunk: number) {
    this.#backfillChunk = backfillChunk;
  }

  // Line 381.
  storeId(): string {
    return this.#storeId ?? '';
  }

  // Lines 1419-1425.
  reconcileBatch(message: EventBatchMessage, context: BatchContext): BatchReconciliation {
    if (context.resumed) return this.#reconcileResumedBatch(message, context.historyFloor);
    return this.#reconcileFreshBatch(message);
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

  // Lines 425-438.
  beginBackfill(stateFloor: number): BackfillRequest | null {
    if (stateFloor === 0) return null;
    return {
      afterSequence: Math.max(0, stateFloor - this.#backfillChunk),
      beforeSequence: stateFloor + 1,
      generation: this.#rebootstrapGeneration,
    };
  }

  // Lines 439-465.
  settleBackfill(
    request: BackfillRequest,
    events: readonly RunEvent[] | undefined,
  ): BackfillReconciliation {
    if (this.#rebootstrapGeneration !== request.generation) return {kind: 'superseded'};
    const filtered = (events ?? []).filter(
      item => item.sequence === undefined || !this.#foldedBelowFloor.has(item.sequence),
    );
    return {
      kind: 'prepend',
      events: filtered,
      historyFloor: this.#lowerHistoryFloor(request.afterSequence),
    };
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
      expect(drive(new StreamReconciler({backfillChunk: 400}), commands)).toEqual(
        drive(new ControllerOracle(400), commands),
      );
    });
  }

  it('agrees across the whole store identity and floor matrix', () => {
    const payloads: readonly EventBatchMessage[] = [
      batch({storeId: 'log', declaredFloor: 100, sequences: [50, 100, 101]}),
      batch({storeId: '', declaredFloor: 100, sequences: [102]}),
      batch({declaredFloor: 100, sequences: [103]}),
      withNulls(batch({sequences: [104]})),
      batch({storeId: 'log', declaredFloor: 0, sequences: [1]}),
      batch({storeId: 'log', declaredFloor: 40, sequences: [20, 40, 41]}),
      batch({storeId: 'other', declaredFloor: 100, sequences: [50, 100]}),
      batch({storeId: 'other', declaredFloor: 900, sequences: [900, 901]}),
    ];
    const chunk = [event(50, SPINE_TYPE), event(100, SPINE_TYPE), event(99), event(undefined)];
    let compared = 0;
    for (const resumed of [false, true]) {
      for (const first of payloads) {
        for (const second of payloads) {
          const commands: readonly Command[] = [
            {op: 'batch', resumed: false, message: first},
            {op: 'begin', id: 0},
            {op: 'batch', resumed, message: second},
            {op: 'settle', id: 0, events: chunk},
            {op: 'begin', id: 1},
            {op: 'settle', id: 1, events: chunk},
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
