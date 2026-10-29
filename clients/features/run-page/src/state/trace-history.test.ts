import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace, traceSpans} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type Journal,
  journalOf,
  openedOn,
  servedPage,
  traceRequest,
  traceSequences,
} from './ended-journal-fixtures.js';
import {type TraceLog, traceAt, traceProblems} from './live.js';
import {control, opened as openedLive, RUN, spanEvent, T0} from './live-run-fixtures.js';
import {
  type RunPageRequest,
  type RunPageState,
  runPageInvariants,
  runPageStep,
} from './run-page.js';
import {scrubberView, viewAtPosition} from './scrub.js';

/**
 * An ended run opened from its summary has one position to scrub to. The page reads the journal's
 * span events before the summary, one page at a time, into the trace the live view folds, so the
 * scrubber's positions and each view are the ones a page that watched the run would have.
 */

interface Reading {
  readonly state: RunPageState;
  /** The requests the page made, in order. */
  readonly asked: readonly RunPageRequest[];
}

/** Read the journal as `sizes` cut it (cycled), until the page asks for no more; `stopAfter` pages at most. */
function read(
  journal: Journal,
  sizes: readonly number[],
  stopAfter = Number.POSITIVE_INFINITY,
): Reading {
  const first = openedOn(journal);
  let state = first.state;
  const asked: RunPageRequest[] = [...first.requests];
  let request = traceRequest(first.requests);
  for (let page = 0; request !== null && page < stopAfter; page += 1) {
    const reply = servedPage(journal, request.from, request.max, sizes[page % sizes.length] ?? 1);
    const step = runPageStep(state, {type: 'trace-page', token: request.token, reply});
    state = step.state;
    asked.push(...step.requests);
    request = traceRequest(step.requests);
  }
  return {state, asked};
}

function reads(asked: readonly RunPageRequest[]) {
  return asked.flatMap(request => (request.kind === 'read-trace' ? [request] : []));
}

/** Every event the trace log holds, the earlier logs' included. */
function heldBy(log: TraceLog): RunEvent[] {
  return [...log.events, ...(log.earlier === null ? [] : heldBy(log.earlier))];
}

function positionsOf(log: TraceLog): number[] {
  return [...log.positions];
}

describe('an ended run opened from its summary', () => {
  test('stands at one position until its history is read', () => {
    const journal = journalOf(new Seeded(7), 30);
    const {state, requests} = openedOn(journal);
    const bar = scrubberView(state.run.trace, {at: 'live'}, true);
    expect(requests.map(request => request.kind)).toEqual(['read-trace']);
    expect(bar?.last).toBe(0);
    expect(bar?.latestLabel).toBe('End');
  });

  for (const seed of SEEDS) {
    test(`reads the same trace whatever the pages' sizes (seed ${seed})`, () => {
      const random = new Seeded(seed);
      const journal = journalOf(random, 1 + random.int(80));
      const sizes = Array.from({length: 1 + random.int(5)}, () => 1 + random.int(40));
      const whole = read(journal, [journal.through]).state.run.trace;
      const paged = read(journal, sizes);
      const trace = paged.state.run.trace;
      // Whatever the chunking, the same positions, planning calls and views as one page of it all.
      expect(positionsOf(trace)).toEqual(positionsOf(whole));
      expect([...trace.calls]).toEqual([...whole.calls]);
      expect(traceProblems(trace)).toEqual([]);
      expect(runPageInvariants(paged.state)).toEqual([]);
      // Every earlier position reads the run as of it: the fold of every span event up to it.
      const sequences = traceSequences(journal);
      for (const at of [sequences[0], sequences[sequences.length >> 1], sequences.at(-1)]) {
        if (at === undefined) continue;
        const expected = foldTrace(
          EMPTY_TRACE,
          journal.events.filter(
            event => event.type.startsWith('span_') && (event.sequence ?? 0) <= at,
          ),
        );
        expect(traceSpans(traceAt(trace, at))).toEqual(traceSpans(expected));
        expect(traceAt(trace, at).sequence).toBe(expected.sequence);
        expect(traceAt(trace, at)).toEqual(traceAt(whole, at));
      }
      // The pages are asked one at a time, each from where the last ended, and the read ends.
      const asks = reads(paged.asked);
      expect(new Set(asks.map(ask => ask.token)).size).toBe(asks.length);
      expect(paged.state.traceHistory).toEqual({kind: 'done'});
      asks.slice(1).forEach((ask, index) => {
        expect(ask.from).toBeGreaterThan(asks[index]?.from ?? 0);
      });
    });
  }

  test('enables the scrubber once a page with older span events arrived, at every position', () => {
    const journal = journalOf(new Seeded(11), 60);
    const before = read(journal, [5], 0);
    expect(scrubberView(before.state.run.trace, {at: 'live'}, true)?.last).toBe(0);
    const after = read(journal, [5], 1);
    const bar = scrubberView(after.state.run.trace, {at: 'live'}, true);
    expect(bar?.last).toBeGreaterThan(0);
    // Moving to an earlier position changes the view; returning to the end follows the run.
    const log = read(journal, [5]).state.run.trace;
    const last = scrubberView(log, {at: 'live'}, true)?.last ?? 0;
    // The start, each span event before the summary's newest event, and the summary's own.
    const older = traceSequences(journal).filter(at => at < journal.through);
    expect(last).toBe(older.length + 1);
    const early = viewAtPosition(log, 1);
    expect(early).not.toEqual({at: 'live'});
    expect(viewAtPosition(log, last)).toEqual({at: 'live'});
  });

  test('stays at one position when nothing before the summary is a span event', () => {
    const others = Array.from({length: 9}, (_, index) => control(index + 1, T0 + index));
    const only = spanEvent(10, T0 + 10, 'span_started', {
      id: 'flow:s1',
      parent: null,
      type: 'flow',
      name: 'step 1',
      status: 'running',
      started_at: T0 + 10,
    } as Span);
    const journal = {events: [...others, only], through: 10};
    const state = read(journal, [4]).state;
    expect(scrubberView(state.run.trace, {at: 'live'}, true)?.last).toBe(0);
    expect(state.traceHistory).toEqual({kind: 'done'});
  });

  test('reads nothing for a journal whose newest event is its first', () => {
    const {requests, state} = openedOn(journalOf(new Seeded(3), 1));
    expect(requests).toEqual([]);
    expect(state.traceHistory).toEqual({kind: 'done'});
  });

  for (const seed of SEEDS.slice(0, 60)) {
    test(`keeps only the events the trace folds, whatever pages carry (seed ${seed})`, () => {
      const random = new Seeded(seed);
      const journal = journalOf(random, 5 + random.int(60));
      const state = read(journal, [1 + random.int(20)]).state;
      const held = heldBy(state.run.trace);
      expect(held.length).toBeGreaterThan(0);
      expect(held.every(event => event.type.startsWith('span_'))).toBe(true);
      expect(held.length).toBeLessThanOrEqual(traceSequences(journal).length);
    });

    test(`keeps the positions it read when a page fails (seed ${seed})`, () => {
      const random = new Seeded(seed);
      const journal = journalOf(random, 10 + random.int(60));
      const good = 1 + random.int(4);
      const size = 1 + random.int(8);
      const reference = read(journal, [size], good);
      const request = traceRequest(reference.asked.slice(-1));
      if (request === null) return; // the read had ended before the failure
      for (const reply of [
        {kind: 'failed', message: 'channel closed'} as const,
        {kind: 'unsupported'} as const,
      ]) {
        const failed = runPageStep(reference.state, {
          type: 'trace-page',
          token: request.token,
          reply,
        });
        expect(positionsOf(failed.state.run.trace)).toEqual(positionsOf(reference.state.run.trace));
        expect(failed.state.run.trace).toBe(reference.state.run.trace);
        expect(failed.requests).toEqual([]);
        expect(failed.state.traceHistory).toEqual({kind: 'done'});
        expect(failed.state.summary.kind).toBe('ready');
      }
    });
  }

  test('stops at a page that makes no progress instead of asking for it again', () => {
    const journal = journalOf(new Seeded(5), 40);
    const {state, requests} = openedOn(journal);
    const request = traceRequest(requests);
    if (request === null) throw new Error('no read asked');
    const stuck = runPageStep(state, {
      type: 'trace-page',
      token: request.token,
      reply: {kind: 'page', events: [], next: request.from, complete: false},
    });
    expect(stuck.requests).toEqual([]);
    expect(stuck.state.traceHistory).toEqual({kind: 'done'});
  });

  test('drops a page that answers a read that is over, or one already answered', () => {
    const journal = journalOf(new Seeded(9), 40);
    const first = openedOn(journal);
    const request = traceRequest(first.requests);
    if (request === null) throw new Error('no read asked');
    const reply = servedPage(journal, request.from, request.max, 4);
    const answered = runPageStep(first.state, {type: 'trace-page', token: request.token, reply});
    const again = runPageStep(answered.state, {type: 'trace-page', token: request.token, reply});
    expect(again.state).toBe(answered.state);
    expect(again.requests).toEqual([]);
  });

  test('lets the history read, when a pane needs the transcript, take over', () => {
    const journal = journalOf(new Seeded(13), 50);
    const first = openedOn(journal);
    const request = traceRequest(first.requests);
    if (request === null) throw new Error('no read asked');
    const needed = runPageStep(first.state, {type: 'need-history'});
    expect(needed.state.traceHistory).toEqual({kind: 'done'});
    expect(needed.requests.map(each => each.kind)).toEqual(['read-history']);
    // The page the trace read still had in flight is dropped.
    const late = runPageStep(needed.state, {
      type: 'trace-page',
      token: request.token,
      reply: servedPage(journal, request.from, request.max, 10),
    });
    expect(late.state.run.trace).toBe(needed.state.run.trace);
    expect(late.requests).toEqual([]);
    expect(runPageInvariants(needed.state)).toEqual([]);
  });

  test('lets go of the read when the run is live again', () => {
    const journal = journalOf(new Seeded(17), 50);
    const first = openedOn(journal);
    const request = traceRequest(first.requests);
    if (request === null) throw new Error('no read asked');
    const live = openedLive();
    const record = live.state.record;
    const resumed = runPageStep(first.state, {type: 'record', record});
    expect(resumed.state.traceHistory).toEqual({kind: 'idle'});
    const late = runPageStep(resumed.state, {
      type: 'trace-page',
      token: request.token,
      reply: servedPage(journal, request.from, request.max, 10),
    });
    expect(late.state.run.trace).toBe(resumed.state.run.trace);
    expect(late.requests).toEqual([]);
    expect(runPageInvariants(resumed.state)).toEqual([]);
  });
});

describe('a live run', () => {
  for (const seed of SEEDS.slice(0, 40)) {
    test(`never reads its history for the scrubber (seed ${seed})`, () => {
      const random = new Seeded(seed);
      const journal = journalOf(random, 3 + random.int(40));
      const page = openedLive();
      // The summary of the same journal, answered to a page whose run is live.
      const step = runPageStep(page.state, {
        type: 'summary',
        token: page.summaryToken,
        wallMs: 0,
        reply: {
          kind: 'summary',
          snapshot: {
            run_id: RUN.runId,
            sequence: journal.through,
            status: 'running',
            spans: [...traceSpans(foldTrace(EMPTY_TRACE, journal.events))],
          },
          seed: [],
          through: journal.through,
          eventCount: journal.through,
          journalBytes: 0,
          trace: 'recorded',
          roundsFinished: 0,
          records: [],
          gates: [],
          terminal: null,
        },
      });
      expect(step.state.summary.kind).toBe('ready');
      expect(step.requests.some(request => request.kind === 'read-trace')).toBe(false);
      expect(step.state.traceHistory).toEqual({kind: 'idle'});
    });
  }
});
