import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunSnapshot} from '@vibesys/contracts';
import {Seeded} from '@vibesys/store/testing';
import {
  attached,
  control,
  events,
  idea,
  MINUTE,
  RUN,
  spanEvent,
  step,
  T0,
} from './live-run-fixtures.js';
import {clockAt} from './run-level.js';
import {createRunReader} from './run-model.js';
import {type RunPageState, runNow, runWallNow} from './run-page.js';
import {scrubberView} from './scrub.js';
import {duration} from './span-reading.js';

/**
 * The live run's clock and durations. The run's spans are on its own clock, which starts wherever
 * its host puts it (seconds from the run's start, or the host's epoch seconds); its events carry
 * the host's wall time. A live run's time is where its newest event puts the run clock, carried
 * forward only by how long this window has waited since it heard that. Regression: the clock added
 * the silence since the last span change on top of the last event's own stamp, and a replayed tail
 * of the run added the run's whole age on top of the snapshot's time, so the clock and every
 * duration ran 40 to 50 minutes ahead of a run that was under an hour old.
 */

/** The run clocks the hosts use: from the run's start, and the host's epoch seconds. */
const ORIGINS = [0, T0, 12_345.5] as const;

/** What the screen says now: the live scrubber's time, and how long the open idea has run. */
function screen(state: RunPageState): {clock: string; ideaFor: string} {
  const now = runNow(state);
  const model = createRunReader().read(state.run.trace, null);
  const start = model.candidates.candidates[0]?.startedAt ?? 0;
  return {
    clock: scrubberView(state.run.trace, state.view, false, runWallNow(state))?.position ?? '',
    ideaFor: duration((clockAt(model, now) ?? start) - start),
  };
}

function timeOfDay(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString().slice(11, 19);
}

describe('the live clock of a run whose step runs for minutes', () => {
  test('reads the host’s time now, not the last event plus the silence since the last span change', () => {
    for (const origin of ORIGINS) {
      // The idea starts at 01:00:00 and changes no span time after; updates keep arriving to 01:40:00.
      const state = step(attached(), [
        events(0, [spanEvent(1, T0, 'span_started', idea(origin))]),
        events(1_000, [spanEvent(2, T0 + 20 * MINUTE, 'span_updated', idea(origin))]),
        events(2_000, [spanEvent(3, T0 + 40 * MINUTE, 'span_updated', idea(origin))]),
        {type: 'tick', wallMs: 2_000 + 17_000},
      ]);
      expect(screen(state)).toEqual({clock: '01:40:17', ideaFor: '40m'});
    }
  });

  test('a replayed tail of the run does not add the run’s age to the snapshot’s time', () => {
    for (const origin of ORIGINS) {
      // The page holds a snapshot whose newest span time is the idea's start, then the stream
      // replays the whole run from its start (stamped 01:00 to 01:45) in one batch.
      const snapshot: RunSnapshot = {
        run_id: RUN.runId,
        sequence: 1,
        status: 'running',
        spans: [idea(origin)],
      };
      const replay = [
        spanEvent(2, T0, 'span_started', idea(origin)),
        ...Array.from({length: 9}, (_, index) => control(3 + index, T0 + 5 * MINUTE * (index + 1))),
      ];
      const state = step(attached(), [
        {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot}},
        {
          type: 'run-data',
          wallMs: 500,
          data: {kind: 'rebootstrap', base: 'preserve', events: replay},
        },
        {type: 'tick', wallMs: 500 + 20_000},
      ]);
      expect(screen(state)).toEqual({clock: '01:45:20', ideaFor: '45m'});
    }
  });

  test('stamps heard after a snapshot, before any span event, move the clock only with this window', () => {
    // Nothing yet says how the snapshot's clock lines up with the events' stamps, so a stamp alone
    // moves nothing: the clock stays where the snapshot put it and goes on with this window's.
    const snapshot: RunSnapshot = {
      run_id: RUN.runId,
      sequence: 1,
      status: 'running',
      spans: [idea(T0)],
    };
    const state = step(attached(), [
      {type: 'run-data', wallMs: 0, data: {kind: 'snapshot', snapshot}},
      events(0, [control(2, T0 + 10 * MINUTE)]),
      {type: 'tick', wallMs: 30_000},
    ]);
    expect(runNow(state)).toBe(T0 + 30);
  });
});

describe('the live clock under clock skew, batching and replays (generated)', () => {
  /** The most the window's clock moves between two arrivals. */
  const MAX_GAP_MS = 30_000;

  interface Delivery {
    readonly origin: number;
    readonly batches: readonly (readonly RunEvent[])[];
    /** The newest stamp (epoch seconds) any event carries, and the batch that first carried it. */
    readonly newest: number;
    readonly firstAt: number;
  }

  const stampOf = (event: RunEvent): number => Date.parse(event.timestamp ?? '') / 1000;

  /** A run's events in order (a start, then updates and controls), cut into batches with replays. */
  function delivery(seed: number): Delivery {
    const random = new Seeded(seed);
    const origin = random.pick(ORIGINS);
    const all: RunEvent[] = [spanEvent(1, T0, 'span_started', idea(origin))];
    let at = T0;
    for (let sequence = 2; sequence <= 6 + random.int(40); sequence += 1) {
      at += random.int(10 * MINUTE);
      all.push(
        random.bool()
          ? spanEvent(sequence, at, 'span_updated', idea(origin))
          : control(sequence, at),
      );
    }
    const batches: RunEvent[][] = [];
    let from = 0;
    while (from < all.length) {
      const size = 1 + random.int(8);
      const fresh = all.slice(from, from + size);
      from += size;
      // A reconnect replays some earlier events in front of the new ones.
      const replayed = from > size && random.bool() ? all.slice(0, random.int(from)) : [];
      batches.push([...replayed, ...fresh]);
    }
    const newest = Math.max(...all.map(stampOf));
    const firstAt = batches.findIndex(batch => batch.some(event => stampOf(event) === newest));
    return {origin, batches, newest, firstAt};
  }

  /** The batches arriving on a window clock that stands at `base` and moves by `gaps`. */
  function arrivals(
    batches: readonly (readonly RunEvent[])[],
    base: number,
    gaps: readonly number[],
  ) {
    let wall = base;
    const each = batches.map((batch, index) => {
      wall += gaps[index] ?? 0;
      return {wall, action: events(wall, batch)};
    });
    return {each, last: wall};
  }

  test('the clock is the newest time the run stamped plus the wait since, on any window clock and run clock', () => {
    for (let seed = 1; seed <= 150; seed += 1) {
      const random = new Seeded(seed * 7919);
      const {origin, batches, newest, firstAt} = delivery(seed);
      const gaps = batches.map(() => 1 + random.int(MAX_GAP_MS));
      const tickAfter = 1 + random.int(MAX_GAP_MS);
      const readings = [0, 1_760_000_000_000, -3_600_000, 86_400_000 * 400].map(base => {
        const {each, last} = arrivals(batches, base, gaps);
        const state = step(attached(), [
          ...each.map(one => one.action),
          {type: 'tick', wallMs: last + tickAfter},
        ]);
        const waited = (last + tickAfter - (each[firstAt]?.wall ?? 0)) / 1000;
        expect(runNow(state)).toBeCloseTo(origin + (newest - T0) + waited, 6);
        expect(runWallNow(state)).toBeCloseTo(newest + waited, 6);
        return {...screen(state), waited};
      });
      // However far this machine's clock is from the run's host, the screen reads the same.
      for (const reading of readings) expect(reading).toEqual(readings[0] as typeof reading);
      expect(readings[0]?.clock).toStartWith(
        timeOfDay(newest + (readings[0]?.waited ?? Number.NaN)),
      );
    }
  });

  test('how the events are cut into batches changes nothing on the screen', () => {
    for (let seed = 1; seed <= 100; seed += 1) {
      const {batches, firstAt} = delivery(seed);
      const gaps = batches.map(() => 1_000);
      const {each, last} = arrivals(batches, 0, gaps);
      // One batch with everything arrives when the first batch with the newest stamp did.
      const whole = step(attached(), [
        events(each[firstAt]?.wall ?? 0, batches.flat()),
        {type: 'tick', wallMs: last + 5_000},
      ]);
      const cut = step(attached(), [
        ...each.map(one => one.action),
        {type: 'tick', wallMs: last + 5_000},
      ]);
      expect(runNow(whole)).toBeCloseTo(runNow(cut) ?? Number.NaN, 6);
    }
  });
});
