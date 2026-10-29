/**
 * The run trace: every span of the run (a flow task or an awaited operation), folded from the
 * run's `span_started`, `span_updated` and `span_ended` events. Each event carries the whole span,
 * so folding is replacing by span id; a span keeps the position it started at, so every span comes
 * after its parent. The snapshot's `spans` is the same fold at the snapshot's sequence: a client
 * starts from it and folds the events after it.
 *
 * Spans are generic: a span's `label` names a kind its search declared, and a kind this client does
 * not know still renders from its name, title, status, attributes and links.
 */
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';

export {closeSpans, isOpen, type RunEnding} from './closing.js';

/** The spans folded so far, in the order they started, and the last sequence folded. */
export interface RunTrace {
  readonly spans: ReadonlyMap<string, Span>;
  readonly sequence: number;
}

/**
 * Reports units of work as a fold does it: one for each span written and one for each span copied
 * to give a trace a store of its own. It lets a test hold the cost of a fold to a bound without
 * timing it.
 */
export type WorkMeter = (units: number) => void;

/**
 * The spans of a run's traces, written once and shared. A trace is a version of the store: the
 * writes before it. Folding from the newest version appends to the store, so a fold costs one write
 * per event and never copies the spans; a trace stays what it was when later traces are folded
 * from it. Folding from an older version is a branch and copies that version into a store of its
 * own.
 */
class SpanStore {
  /** Every span event's span, in the order folded. */
  readonly writes: Span[] = [];
  /** Where each span's first write is, in the order spans started. */
  readonly first = new Map<string, number>();
  /** Where each span's latest write is. */
  readonly latest = new Map<string, number>();

  write(span: Span): void {
    const at = this.writes.length;
    this.writes.push(span);
    if (!this.first.has(span.id)) this.first.set(span.id, at);
    this.latest.set(span.id, at);
  }

  /** The spans as of the first `version` writes, in the order they started. */
  spansAt(version: number): Map<string, Span> {
    const spans = new Map<string, Span>();
    if (version === this.writes.length) {
      for (const id of this.first.keys()) {
        spans.set(id, this.writes[this.latest.get(id) as number] as Span);
      }
      return spans;
    }
    for (let at = 0; at < version; at += 1) {
      const span = this.writes[at] as Span;
      spans.set(span.id, span);
    }
    return spans;
  }
}

class FoldedTrace implements RunTrace {
  readonly store: SpanStore;
  readonly version: number;
  readonly sequence: number;
  #spans: Map<string, Span> | null = null;

  constructor(store: SpanStore, version: number, sequence: number) {
    this.store = store;
    this.version = version;
    this.sequence = sequence;
  }

  /** The spans of this version, read from the store once. */
  get spans(): ReadonlyMap<string, Span> {
    this.#spans ??= this.store.spansAt(this.version);
    return this.#spans;
  }

  /** Whether no trace has been folded from this store since this one. */
  get newest(): boolean {
    return this.version === this.store.writes.length;
  }
}

/** The trace before any event. */
export const EMPTY_TRACE: RunTrace = new FoldedTrace(new SpanStore(), 0, 0);

const SPAN_EVENTS: ReadonlySet<string> = new Set(['span_started', 'span_updated', 'span_ended']);

/** The trace a snapshot carries, at the snapshot's sequence. */
export function traceFromSnapshot(snapshot: RunSnapshot): RunTrace {
  const store = new SpanStore();
  for (const span of snapshot.spans ?? []) store.write(span);
  return new FoldedTrace(store, store.writes.length, snapshot.sequence);
}

/**
 * `trace` with `events` folded in order. Events at or before the trace's sequence are already in
 * it (a snapshot taken after them) and are skipped; events of other types leave the spans as they
 * are. Folding costs one unit of work per span event, however many spans the trace holds, when
 * `trace` is the newest fold of its spans (the way a run is followed); folding from an older trace
 * copies its spans once.
 */
export function foldTrace(
  trace: RunTrace,
  events: Iterable<RunEvent>,
  meter: WorkMeter = noMeter,
): RunTrace {
  const from = ownedBy(trace, meter);
  let store: SpanStore | null = null;
  let sequence = from.sequence;
  for (const event of events) {
    const at = event.sequence ?? 0;
    if (at <= sequence) continue;
    sequence = at;
    const span = spanOf(event);
    if (span === null) continue;
    store ??= from.newest ? from.store : copyOf(from, meter);
    store.write(span);
    meter(1);
  }
  if (sequence === from.sequence) return from;
  return store === null
    ? new FoldedTrace(from.store, from.version, sequence)
    : new FoldedTrace(store, store.writes.length, sequence);
}

/** The trace's spans in the order they started. */
export function traceSpans(trace: RunTrace): readonly Span[] {
  return [...trace.spans.values()];
}

function noMeter(): void {}

/** `trace` as a folded trace; a trace built some other way is copied into a store once. */
function ownedBy(trace: RunTrace, meter: WorkMeter): FoldedTrace {
  if (trace instanceof FoldedTrace) return trace;
  const store = new SpanStore();
  for (const span of trace.spans.values()) store.write(span);
  meter(store.writes.length);
  return new FoldedTrace(store, store.writes.length, trace.sequence);
}

/** A store of its own holding the spans of `trace`, for a fold that branches from it. */
function copyOf(trace: FoldedTrace, meter: WorkMeter): SpanStore {
  const store = new SpanStore();
  for (const span of trace.spans.values()) store.write(span);
  meter(store.writes.length);
  return store;
}

function spanOf(event: RunEvent): Span | null {
  if (!SPAN_EVENTS.has(event.type)) return null;
  const data = event.data;
  return data !== null && data !== undefined && 'span' in data ? data.span : null;
}
