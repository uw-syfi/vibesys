import {expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {placeLabels} from '@vibesys/ui';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';
import {scrubberView} from './scrub.js';

/**
 * The scrubber's planning-call ticks over the 14-day run (fifteen calls, the last ones close
 * together): at any width the track has at a window's size, every call is labelled, in order, so
 * the numbers never skip.
 */
test('every planning call of a long run is labelled on the track, in order, at any window width', () => {
  const log = traceStep(EMPTY_TRACE_LOG, {
    kind: 'events',
    events: fixtureNamed('weeks-600-ideas').events,
  });
  const bar = scrubberView(log, {at: 'live'}, false);
  const ticks = bar?.ticks ?? [];
  expect(ticks.length).toBeGreaterThan(10);
  expect(ticks.map(tick => tick.label)).toEqual(ticks.map((_, index) => String(index + 1)));
  for (const width of [640, 800, 1000, 1440]) {
    const placed = placeLabels(
      ticks.map(tick => ({share: tick.at / (bar?.last ?? 1), text: tick.label})),
      width,
    );
    expect({width, labelled: placed.length}).toEqual({width, labelled: ticks.length});
  }
});
