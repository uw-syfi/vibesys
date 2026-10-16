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

/** The spans folded so far, in the order they started, and the last sequence folded. */
export interface RunTrace {
  readonly spans: ReadonlyMap<string, Span>;
  readonly sequence: number;
}

/** The trace before any event. */
export const EMPTY_TRACE: RunTrace = {spans: new Map(), sequence: 0};

const SPAN_EVENTS: ReadonlySet<string> = new Set(['span_started', 'span_updated', 'span_ended']);

/** The trace a snapshot carries, at the snapshot's sequence. */
export function traceFromSnapshot(snapshot: RunSnapshot): RunTrace {
  const spans = new Map<string, Span>();
  for (const span of snapshot.spans ?? []) spans.set(span.id, span);
  return {spans, sequence: snapshot.sequence};
}

/**
 * `trace` with `events` folded in order. Events at or before the trace's sequence are already in
 * it (a snapshot taken after them) and are skipped; events of other types leave the spans as they
 * are.
 */
export function foldTrace(trace: RunTrace, events: Iterable<RunEvent>): RunTrace {
  let spans: Map<string, Span> | null = null;
  let sequence = trace.sequence;
  for (const event of events) {
    const at = event.sequence ?? 0;
    if (at <= sequence) continue;
    sequence = at;
    const span = spanOf(event);
    if (span === null) continue;
    spans ??= new Map(trace.spans);
    spans.set(span.id, span);
  }
  return sequence === trace.sequence ? trace : {spans: spans ?? trace.spans, sequence};
}

/** The trace's spans in the order they started. */
export function traceSpans(trace: RunTrace): readonly Span[] {
  return [...trace.spans.values()];
}

function spanOf(event: RunEvent): Span | null {
  if (!SPAN_EVENTS.has(event.type)) return null;
  const data = event.data;
  return data !== null && data !== undefined && 'span' in data ? data.span : null;
}
