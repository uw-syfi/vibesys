import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  HISTORY_IN_FLIGHT,
  type HistoryPage,
  type HistoryPager,
  type PageRequest,
  receivePage,
  startHistory,
} from './history-pager.js';

/** One journal event: its sequence and its size in bytes on disk. */
interface Line {
  readonly sequence: number;
  readonly bytes: number;
}

/** A journal of `size` events whose sequences may skip numbers and whose sizes vary widely. */
function journalOf(random: Seeded, size: number): readonly Line[] {
  const lines: Line[] = [];
  let sequence = 1;
  for (let index = 0; index < size; index += 1) {
    lines.push({sequence, bytes: random.int(10) === 0 ? 1 + random.int(5000) : 1 + random.int(80)});
    sequence += random.int(6) === 0 ? 2 + random.int(3) : 1;
  }
  return lines;
}

const event = (line: Line): RunEvent => ({sequence: line.sequence}) as unknown as RunEvent;

/**
 * The helper's page (`event_page`): events from `from` on, at most `max`, stopping before the event
 * that would pass `maxBytes` unless it is the page's first; `next` follows the last event returned.
 */
function serve(
  journal: readonly Line[],
  request: PageRequest,
  maxBytes: number,
  totals: boolean,
  reportedEvents: number,
): HistoryPage {
  const events: RunEvent[] = [];
  let size = 0;
  let next = request.from;
  let complete = true;
  for (const line of journal) {
    if (line.sequence < request.from) continue;
    if (events.length === request.max || (events.length > 0 && size + line.bytes > maxBytes)) {
      complete = false;
      break;
    }
    events.push(event(line));
    size += line.bytes;
    next = line.sequence + 1;
  }
  const totalBytes = journal.reduce((sum, line) => sum + line.bytes, 0);
  return {
    events,
    next,
    complete,
    ...(totals ? {totalEvents: reportedEvents, totalBytes} : {}),
  };
}

interface Outcome {
  readonly read: readonly number[];
  readonly requests: number;
  readonly done: boolean;
  readonly peakInFlight: number;
}

/** Read `journal` through the pager, answering requests in a random order. */
function readAll(random: Seeded, journal: readonly Line[]): Outcome {
  const pageEvents = 1 + random.int(40);
  const maxBytes = 1 + random.int(600);
  const totals = random.bool();
  // A live journal grows after the first page: the total it named may be short, or (never in
  // practice, but harmless) long.
  const reported = Math.max(0, journal.length + random.int(7) - 3);
  const read: number[] = [];
  let pending: PageRequest[] = [];
  let requests = 0;
  let peak = 0;
  let done = false;
  const take = (step: {
    pager: HistoryPager;
    events: readonly RunEvent[];
    requests: readonly PageRequest[];
    nextToken: number;
    done: boolean;
  }): HistoryPager => {
    read.push(...step.events.flatMap(each => (each.sequence === undefined ? [] : [each.sequence])));
    pending.push(...step.requests);
    requests += step.requests.length;
    peak = Math.max(peak, pending.length);
    done = step.done;
    return step.pager;
  };
  let token = 1;
  const started = startHistory(1, pageEvents, token);
  token = started.nextToken;
  let pager = take(started);
  while (pending.length > 0 && !done) {
    const [request] = pending.splice(random.int(pending.length), 1) as [PageRequest];
    const page = serve(journal, request, maxBytes, totals, reported);
    const step = receivePage(pager, request.token, page, pageEvents, token);
    if (step === null) throw new Error(`no span waits on token ${request.token}`);
    token = step.nextToken;
    pager = take(step);
    if (requests > 10 * (journal.length + 5)) throw new Error('the pager is not converging');
  }
  pending = [];
  return {read, requests, done, peakInFlight: peak};
}

describe('reading a history through the pager', () => {
  test('hands out the journal in order, each event once, for any bounds and arrival order', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const journal = journalOf(random, random.int(400));
      const outcome = readAll(random, journal);
      expect(outcome.done, `seed ${seed}`).toBe(true);
      expect(outcome.read, `seed ${seed}`).toEqual(journal.map(line => line.sequence));
      expect(outcome.peakInFlight, `seed ${seed}`).toBeLessThanOrEqual(HISTORY_IN_FLIGHT);
    }
  });

  test('asks for several pages at once once the journal size is known, and never before', () => {
    const journal = journalOf(new Seeded(7), 300);
    const first = startHistory(1, 50, 1);
    expect(first.requests).toHaveLength(1);
    const reply = serve(journal, first.requests[0] as PageRequest, 400, true, journal.length);
    const step = receivePage(first.pager, 1, reply, 50, first.nextToken);
    expect(step?.requests.length).toBe(HISTORY_IN_FLIGHT);
    const unknown = serve(journal, first.requests[0] as PageRequest, 400, false, journal.length);
    expect(receivePage(first.pager, 1, unknown, 50, first.nextToken)?.requests).toHaveLength(1);
  });

  test('records how far the history is read and what the host says the journal holds', () => {
    const journal = journalOf(new Seeded(3), 120);
    const first = startHistory(1, 10, 1);
    const reply = serve(journal, first.requests[0] as PageRequest, 10_000, true, journal.length);
    const step = receivePage(first.pager, 1, reply, 10, first.nextToken);
    expect(step?.pager.read).toBe(reply.events.length);
    expect(step?.pager.total).toEqual({events: 120, bytes: reply.totalBytes ?? null});
  });

  test('drops a reply nothing waits for, and one for a page it already took', () => {
    const journal = journalOf(new Seeded(11), 30);
    const first = startHistory(1, 5, 1);
    const reply = serve(journal, first.requests[0] as PageRequest, 10_000, false, 30);
    expect(receivePage(first.pager, 99, reply, 5, first.nextToken)).toBeNull();
    const taken = receivePage(first.pager, 1, reply, 5, first.nextToken);
    expect(taken === null || receivePage(taken.pager, 1, reply, 5, taken.nextToken)).toBeNull();
  });

  test('stops, rather than ask again, when a page cannot advance', () => {
    const first = startHistory(1, 5, 1);
    const stuck: HistoryPage = {events: [], next: 1, complete: false};
    const step = receivePage(first.pager, 1, stuck, 5, first.nextToken);
    expect(step).toMatchObject({done: true, requests: []});
  });

  test('keeps an overlapping reply from duplicating or reordering events', () => {
    // Regression guard for the pipelining: a span's reply may carry events past its end (the
    // helper counts events, not sequences), and those belong to the next span.
    const journal = journalOf(new Seeded(5), 200);
    for (const seed of SEEDS.slice(0, 60)) {
      const outcome = readAll(new Seeded(seed), journal);
      expect(outcome.read).toEqual(journal.map(line => line.sequence));
    }
  });
});
