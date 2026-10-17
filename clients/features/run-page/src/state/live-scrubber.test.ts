import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import {traceSpans} from '@vibesys/run-state/trace';
import {Seeded} from '@vibesys/store/testing';
import {EMPTY_TRACE_LOG, type RunData, traceAt, traceStep} from './live.js';
import {scrubberView, viewAtPosition} from './scrub.js';

/**
 * A live run for hours: its span events keep arriving while the connection recovers now and then
 * (a replacement snapshot, or a stream replay). Regression: a recovery replaced the trace log with
 * one that held only the snapshot, so the scrubber's range collapsed to 0 and its thumb and clock
 * stood still while the run was still live ("Live", range 0, cannot be dragged).
 */

const START_MS = Date.UTC(2026, 9, 17, 6, 0, 0);
const SECONDS_PER_EVENT = 6;

function spanOf(n: number): Span {
  return {
    id: `s${n}`,
    parent: null,
    type: 'flow',
    name: `step ${n}`,
    status: 'running',
    started_at: START_MS / 1000 + n * SECONDS_PER_EVENT,
  };
}

function eventAt(sequence: number): RunEvent {
  return {
    sequence,
    timestamp: new Date(START_MS + sequence * SECONDS_PER_EVENT * 1000).toISOString(),
    type: 'span_started',
    data: {kind: 'span_started', span: spanOf(sequence)},
  } as RunEvent;
}

function snapshotAt(sequence: number): RunSnapshot {
  return {
    run_id: 'run',
    sequence,
    status: 'running',
    spans: Array.from({length: sequence}, (_, index) => spanOf(index + 1)),
  };
}

/** Generated delivery of a run of `total` events: batches, with recoveries between some of them. */
function deliveries(seed: number, total: number): {data: RunData; through: number}[] {
  const random = new Seeded(seed);
  const out: {data: RunData; through: number}[] = [];
  let sequence = 0;
  while (sequence < total) {
    const size = Math.min(1 + random.int(40), total - sequence);
    const events = Array.from({length: size}, () => eventAt(++sequence));
    out.push({data: {kind: 'events', events}, through: sequence});
    const recovery = random.int(10);
    if (recovery === 0) {
      out.push({data: {kind: 'snapshot', snapshot: snapshotAt(sequence)}, through: sequence});
    } else if (recovery === 1) {
      const tail = events.slice(-1 - random.int(size));
      out.push({
        data: {kind: 'rebootstrap', base: 'preserve', events: tail},
        through: sequence,
      });
    }
  }
  return out;
}

describe('the live time bar over a long run', () => {
  for (const seed of [1, 2, 3, 4, 5, 6, 7, 8]) {
    test(`keeps its range, thumb and clock advancing through recoveries (seed ${seed})`, () => {
      // Thousands of events, six seconds apart: hours of a run.
      const total = 4000;
      let log = EMPTY_TRACE_LOG;
      let range = 0;
      let seenAt = '';
      for (const {data, through} of deliveries(seed, total)) {
        log = traceStep(log, data);
        const bar = scrubberView(log, {at: 'live'}, false);
        expect(bar).not.toBeNull();
        if (bar === null) return;
        // The range never shrinks while the run is live, and the thumb sits at its end.
        expect(bar.last).toBeGreaterThanOrEqual(range);
        expect(bar.value).toBe(bar.last);
        range = bar.last;
        // The clock stands at the newest event's time of day.
        const at = new Date(START_MS + through * SECONDS_PER_EVENT * 1000)
          .toISOString()
          .slice(11, 19);
        expect(bar.position.startsWith(at)).toBe(true);
        seenAt = bar.position;
      }
      expect(range).toBeGreaterThanOrEqual(total - 1);
      expect(seenAt.startsWith('12:40:00')).toBe(true);
    });
  }

  test('a past position stays draggable to and reads what the page had folded then', () => {
    let log = traceStep(EMPTY_TRACE_LOG, {
      kind: 'events',
      events: Array.from({length: 30}, (_, index) => eventAt(index + 1)),
    });
    log = traceStep(log, {kind: 'snapshot', snapshot: snapshotAt(30)});
    log = traceStep(log, {kind: 'events', events: [eventAt(31)]});
    const bar = scrubberView(log, {at: 'live'}, false);
    expect(bar?.last).toBe(31);
    for (const index of [1, 5, 29, 30]) {
      const view = viewAtPosition(log, index);
      const at = view.at === 'live' ? null : view.at;
      expect(traceSpans(traceAt(log, at)).length).toBeGreaterThan(0);
    }
    // An early position shows the early trace, not the snapshot's.
    const early = viewAtPosition(log, 4);
    expect(early.at).not.toBe('live');
    expect(traceSpans(traceAt(log, early.at === 'live' ? null : early.at)).length).toBeLessThan(10);
  });
});
