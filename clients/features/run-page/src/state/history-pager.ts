/**
 * Reading an ended run's recorded events with several pages in flight, folded strictly in order.
 *
 * A page is `runs.read_events` from a sequence: up to a count of events, cut earlier at the
 * helper's byte bound, so where a page ends is known only when its reply arrives. The pager keeps
 * a queue of sequence spans `[from, to)` that together cover the journal. The first request is
 * open-ended; its reply names the journal's size (when the host does), and the rest of the
 * journal is cut into spans as large as that first page was, with up to `HISTORY_IN_FLIGHT`
 * requested at once. A reply that stops short of its span's end (the byte bound) leaves a
 * remainder span at the head of the queue.
 *
 * The invariants, which the tests check against any journal, page bounds and arrival order:
 * - events are handed out in journal order, each once: a span's events are cut at its end and
 *   anything below the frontier (the next sequence owed) is dropped, so a reply that overlaps
 *   another's range cannot duplicate or reorder;
 * - the pager finishes only when a reply says the journal is complete, or cannot advance;
 * - nothing here reads a clock or does I/O: replies in, events and requests out.
 */

import type {RunEvent} from '@vibesys/contracts';

/** The most spans requested and not yet answered. */
export const HISTORY_IN_FLIGHT = 3;

/**
 * The fewest events a planned span holds, so a first page that held one huge event does not cut
 * the journal into thousands of spans; a span longer than a page is read in several.
 */
const MIN_SPAN_EVENTS = 100;

/** One page of a run's history as the host answered it. */
export interface HistoryPage {
  readonly events: readonly RunEvent[];
  /** The sequence the next page starts at. */
  readonly next: number;
  /** The page reached the end of what the journal holds now. */
  readonly complete: boolean;
  /** The journal's events when the page was read, when the host says so. */
  readonly totalEvents?: number;
  /** The journal's bytes when the page was read, when the host says so. */
  readonly totalBytes?: number;
  /** The page's size on the wire in bytes, when the app measured it. */
  readonly bytes?: number;
}

/** A sequence span the history still owes: asked under `token`, and answered with `page`. */
interface Span {
  readonly from: number;
  /** Exclusive; `Infinity` until the journal's size is known. */
  readonly to: number;
  readonly token: number | null;
  readonly page: HistoryPage | null;
}

/** A page to ask for: `max` events from `from`, answered under `token`. */
export interface PageRequest {
  readonly token: number;
  readonly from: number;
  readonly max: number;
}

export interface HistoryPager {
  /** The next sequence the fold is owed: events below it were handed out or dropped. */
  readonly frontier: number;
  /** The spans left, in journal order; the first is the next to fold. */
  readonly spans: readonly Span[];
  /** Events handed out so far. */
  readonly read: number;
  /** What the host last said the journal holds; null while it has not said. */
  readonly total: {readonly events: number; readonly bytes: number | null} | null;
  /** The journal is already cut into spans. */
  readonly planned: boolean;
}

export interface PagerStep {
  readonly pager: HistoryPager;
  /** The events now ready to fold, in journal order and none seen before. */
  readonly events: readonly RunEvent[];
  readonly requests: readonly PageRequest[];
  /** The next unused token. */
  readonly nextToken: number;
  /** The whole history was read (or can read no further). */
  readonly done: boolean;
}

/** Start reading at sequence `from` with `pageEvents` events per page, using tokens from `token`. */
export function startHistory(from: number, pageEvents: number, token: number): PagerStep {
  const pager: HistoryPager = {
    frontier: from,
    spans: [{from, to: Infinity, token: null, page: null}],
    read: 0,
    total: null,
    planned: false,
  };
  return asking(pager, [], pageEvents, token, false);
}

/**
 * Take the reply to the page asked under `token`. Null when no span waits on `token`: the reading
 * it belonged to is over, and the reply is dropped.
 */
export function receivePage(
  pager: HistoryPager,
  token: number,
  page: HistoryPage,
  pageEvents: number,
  nextToken: number,
): PagerStep | null {
  if (!pager.spans.some(span => span.token === token && span.page === null)) return null;
  const answered: HistoryPager = {
    ...pager,
    spans: pager.spans.map(span => (span.token === token ? {...span, page} : span)),
  };
  const folded = fold(answered);
  return asking(folded.pager, folded.events, pageEvents, nextToken, folded.done);
}

interface Folded {
  readonly pager: HistoryPager;
  readonly events: readonly RunEvent[];
  readonly done: boolean;
}

/** Fold the answered spans at the head of the queue, in order, for as long as there are any. */
function fold(start: HistoryPager): Folded {
  let pager = start;
  const events: RunEvent[] = [];
  for (;;) {
    const [head, ...rest] = pager.spans;
    if (head?.page == null) return {pager, events, done: false};
    const {page} = head;
    const heard = hear(pager, head, page, events);
    // A page that read nothing new cannot advance: stop rather than ask for the same page again.
    if (page.complete || page.next <= head.from) {
      return {pager: {...heard, spans: []}, events, done: true};
    }
    if (page.next < head.to) {
      // The page stopped short of its span (the byte bound, or an open span): the rest is owed.
      const remainder: Span = {from: page.next, to: head.to, token: null, page: null};
      pager = planned({...heard, spans: [remainder, ...rest]}, page.events.length);
      continue;
    }
    pager = {...heard, frontier: Math.max(heard.frontier, head.to), spans: rest};
  }
}

/** `pager` having taken `page`'s events into `events`: the new ones inside `head`'s span. */
function hear(
  pager: HistoryPager,
  head: Span,
  page: HistoryPage,
  events: RunEvent[],
): HistoryPager {
  let frontier = pager.frontier;
  let read = pager.read;
  for (const event of page.events) {
    // The helper returns only events that carry an integer sequence.
    const {sequence} = event;
    if (sequence === undefined || sequence < frontier || sequence >= head.to) continue;
    events.push(event);
    read += 1;
    frontier = sequence + 1;
  }
  const total =
    page.totalEvents === undefined
      ? pager.total
      : {events: page.totalEvents, bytes: page.totalBytes ?? null};
  return {...pager, frontier, read, total};
}

/**
 * Cut an open-ended head span into spans of `size` events once the journal's size is known, the
 * last one open-ended so a journal that grew since is still read to its end. `size` is what the
 * page just read held: a page is as long as the host lets one be, so spans of that length ask for
 * about one page each.
 */
function planned(pager: HistoryPager, size: number): HistoryPager {
  const [head, ...rest] = pager.spans;
  if (pager.planned || pager.total === null || head === undefined || head.to !== Infinity) {
    return pager;
  }
  const length = Math.max(MIN_SPAN_EVENTS, size);
  const remaining = pager.total.events - pager.read;
  const spans: Span[] = [];
  let from = head.from;
  while (remaining - (from - head.from) > length) {
    spans.push({from, to: from + length, token: null, page: null});
    from += length;
  }
  spans.push({from, to: Infinity, token: null, page: null});
  return {...pager, planned: true, spans: [...spans, ...rest]};
}

/** Ask for the spans not yet asked, while fewer than `HISTORY_IN_FLIGHT` are waiting. */
function asking(
  pager: HistoryPager,
  events: readonly RunEvent[],
  pageEvents: number,
  nextToken: number,
  done: boolean,
): PagerStep {
  if (done) return {pager, events, requests: [], nextToken, done};
  let waiting = pager.spans.filter(span => span.token !== null && span.page === null).length;
  let token = nextToken;
  const requests: PageRequest[] = [];
  const spans = pager.spans.map(span => {
    if (span.token !== null || waiting >= HISTORY_IN_FLIGHT) return span;
    waiting += 1;
    const max = Math.min(pageEvents, span.to - span.from);
    requests.push({token, from: span.from, max});
    return {...span, token: token++};
  });
  return {pager: {...pager, spans}, events, requests, nextToken: token, done};
}
