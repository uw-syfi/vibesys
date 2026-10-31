import {expect, test} from 'bun:test';
import {fromBinary} from '@bufbuild/protobuf';
import {ClientReplaySchema, RecordSchema} from '@vibesys/proto';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {derivedReplay} from '@vibesys/run-fixtures/derived';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {placeLabels} from '@vibesys/ui';
import {projectSkeleton, runPageInitial, scrubberView, skeletonScreenState} from './index.js';
import {EMPTY_TRACE_LOG, traceStep} from './live.js';

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

test('native dense planning calls fit every numeric label across track widths', () => {
  const replay = fromBinary(ClientReplaySchema, derivedReplay('shared', 'weeks-600-ideas'));
  const records = replay.batches.flatMap(batch =>
    batch.records.map(bytes => fromBinary(RecordSchema, bytes)),
  );
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const seed = {
    identity: {host: 'local' as const, runId: 'run-a'},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'Local',
    unavailable: {},
  };
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  const state = skeletonScreenState(seed, skeleton, model, runPageInitial(seed));
  const bar = scrubberView(state.run.trace, {at: 'live'}, true);
  if (bar === null) throw new Error('Canonical dense run requires a scrubber');
  expect(bar.ticks).toHaveLength(15);
  for (let width = 500; width <= 1400; width += 20) {
    const labels = bar.ticks.map(tick => ({share: tick.at / bar.last, text: tick.label}));
    expect(placeLabels(labels, width)).toHaveLength(15);
  }
  const worded = bar.ticks.map(tick => ({share: tick.at / bar.last, text: `Call ${tick.label}`}));
  expect(placeLabels(worded, 1344).length).toBeLessThan(15);
});
