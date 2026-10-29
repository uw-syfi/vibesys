import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {attached, events, isoAt, opened, RUN, step, T0} from './live-run-fixtures.js';
import {type RunPageAction, type RunPageState, runPageStep} from './run-page.js';
import {summaryFacts} from './run-summary.js';
import {scrubberView, viewAtPosition} from './scrub.js';

/**
 * A live run opened mid-way: the page asks for the run's summary and attaches at once, and either
 * can answer first. The session's replay carries the run's recent span events; the summary carries
 * the run up to its newest event. Whichever answers first, the time bar must reach back through
 * every event the page heard, not only through those after the summary's end.
 *
 * Regression: a summary that landed first became the trace's base at its newest event, and the
 * session's replay of events at or before that base was dropped as already folded, so the bar held
 * one position (disabled, "Live", a track that cannot be moved) however long the run had worked.
 */

function span(n: number): Span {
  return {
    id: `flow:s${n}`,
    parent: null,
    type: 'flow',
    name: `step ${n}`,
    status: 'running',
    started_at: T0 + n * 6,
  } as Span;
}

function started(n: number): RunEvent {
  return {
    protocol_version: 1,
    run_id: RUN.runId,
    sequence: n,
    timestamp: isoAt(T0 + n * 6),
    type: 'span_started',
    data: {kind: 'span_started', span: span(n)},
  } as RunEvent;
}

/** The summary the host sends for a journal of span events 1 to `through`. */
function summaryAt(through: number): RunPageAction {
  const none = {input: null, cacheRead: null, cacheWrite: null, output: null, reasoning: null};
  return {
    type: 'summary',
    token: 0,
    wallMs: 0,
    reply: {
      kind: 'summary',
      ...summaryFacts(
        {
          throughSequence: through,
          eventCount: through,
          journalBytes: 0,
          trace: 'recorded',
          roundsFinished: 0,
          overview: null,
          spans: Array.from({length: through}, (_, index) => span(index + 1)),
          replies: [],
          tokens: none,
          usage: [],
          records: [],
          inputGates: [],
          terminal: null,
        },
        RUN.runId,
        'running',
      ),
    },
  };
}

function bar(state: RunPageState) {
  return scrubberView(state.run.trace, {at: 'live'}, false);
}

describe('the time bar of a live run opened mid-way', () => {
  for (const seed of SEEDS) {
    test(`reaches back through the session's replay, whichever answers first (seed ${seed})`, () => {
      const random = new Seeded(seed);
      const total = 5 + random.int(60);
      const replay = Array.from({length: total}, (_, index) => started(index + 1));
      const page = opened();
      const summary = {...summaryAt(total), token: page.summaryToken};
      const batch = events(0, replay);
      const orders = [
        [page.attached, summary, batch],
        [page.attached, batch, summary],
        [summary, page.attached, batch],
      ];
      for (const [which, order] of orders.entries()) {
        const state = step(page.state, order);
        const shown = bar(state);
        expect(shown?.last, `seed ${seed} order ${which}`).toBeGreaterThanOrEqual(total - 1);
        // Dragging to any position reads the run as of it, and returning to the end is live.
        const at = random.int(total);
        const view = viewAtPosition(state.run.trace, at);
        const past = runPageStep(state, {type: 'view', view}).state;
        expect(scrubberView(past.run.trace, view, false)?.value).toBe(at);
        expect(viewAtPosition(state.run.trace, shown?.last ?? 0)).toEqual({at: 'live'});
      }
    });
  }

  test('a page attached with nothing to replay still has one position, not a broken bar', () => {
    const state = attached();
    expect(bar(state)).toBeNull();
  });
});
