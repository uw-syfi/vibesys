/**
 * An ended run's scrubber reach: reading the span events before the run's summary into its trace.
 *
 * A summary stands at the run's newest event and its spans carry no sequences, so a page opened
 * from one has a single position to scrub to. The positions are the sequences of the trace's span
 * events (`TraceLog`), so the older ones come from the journal: the page reads it forward from its
 * first event, one page at a time, keeps the trace's events of each page (`traceEventsOf`) and
 * hands them to the trace fold (`traceStep`), which reads those before its base into the positions
 * and the earlier views. Forward is the only order that keeps every position's view exact: a view
 * at a sequence is the fold of every span event up to it, so the events before a position must be
 * known before it can be stood at.
 *
 * Nothing else of a page is kept, so the memory the read holds is the trace's alone, not the
 * journal's. The read stops at the first page that is the journal's end, makes no progress, or
 * reaches the summary's sequence; a page that fails or is not served ends it too, with the
 * positions read so far standing. Nothing here does I/O: replies in, the next request out.
 */
import type {RunEvent} from '@vibesys/contracts';
import {type TraceLog, traceEventsOf, traceStep} from './live.js';

/** What one page read settled to (`HistoryReply` is one). */
type TracePageReply =
  | {
      readonly kind: 'page';
      readonly events: readonly RunEvent[];
      readonly next: number;
      readonly complete: boolean;
    }
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'failed'; readonly message: string};

/** The read of the trace's earlier events: not asked yet, being read, or over (for any reason). */
export type TraceHistory =
  | {readonly kind: 'idle'}
  | {
      readonly kind: 'reading';
      /** The token of the page being read. */
      readonly token: number;
      /** The sequence the page being read starts at. */
      readonly from: number;
      /** The sequence of the summary's newest event: the base, so events from it on are not wanted. */
      readonly through: number;
    }
  | {readonly kind: 'done'};

/** A page to ask for: `max` events from `from`, answered under `token`. */
export interface TracePageRequest {
  readonly token: number;
  readonly from: number;
  readonly max: number;
}

export interface TraceHistoryStep {
  readonly load: TraceHistory;
  readonly trace: TraceLog;
  readonly request: TracePageRequest | null;
  /** The next unused token. */
  readonly nextToken: number;
}

/** The journal's first sequence, where the read starts. */
const FIRST_SEQUENCE = 1;

/**
 * Start reading the events before `through` (the summary's newest sequence), asking the first page
 * under `token`; nothing to read when no event comes before it.
 */
export function startTraceHistory(
  through: number,
  max: number,
  token: number,
): {readonly load: TraceHistory; readonly request: TracePageRequest | null} {
  if (through <= FIRST_SEQUENCE) return {load: {kind: 'done'}, request: null};
  return {
    load: {kind: 'reading', token, from: FIRST_SEQUENCE, through},
    request: {token, from: FIRST_SEQUENCE, max},
  };
}

/**
 * Take the reply to the page asked under `token`: its trace events are folded into `trace`, and
 * the next page is asked for unless the read is over. Null when no page waits on `token`: the read
 * it belonged to is over, and the reply is dropped.
 */
export function receiveTracePage(
  load: TraceHistory,
  trace: TraceLog,
  token: number,
  reply: TracePageReply,
  max: number,
  nextToken: number,
): TraceHistoryStep | null {
  if (load.kind !== 'reading' || load.token !== token) return null;
  const over: TraceHistoryStep = {load: {kind: 'done'}, trace, request: null, nextToken};
  if (reply.kind !== 'page') return over;
  const events = traceEventsOf(reply.events);
  const folded = events.length === 0 ? trace : traceStep(trace, {kind: 'events', events});
  // A page that read nothing new cannot advance: stop rather than ask for the same page again.
  if (reply.complete || reply.next <= load.from || reply.next >= load.through) {
    return {...over, trace: folded};
  }
  return {
    load: {kind: 'reading', token: nextToken, from: reply.next, through: load.through},
    trace: folded,
    request: {token: nextToken, from: reply.next, max},
    nextToken: nextToken + 1,
  };
}
