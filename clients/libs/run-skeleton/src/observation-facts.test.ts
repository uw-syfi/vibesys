import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {LifecycleSchema, RecordSchema, RunHeadSchema, RunLiveness} from '@vibesys/proto';
import {foldSkeleton, observeRun, seedSkeleton} from '@vibesys/run-skeleton';
import fc from 'fast-check';

test('property: the page and controls observe the newest epoch-fenced committed lifecycle', () => {
  fc.assert(
    fc.property(
      fc.integer({min: 1, max: 8}),
      fc.integer({min: 1, max: 8}),
      fc.constantFrom('starting', 'running', 'parked'),
      fc.boolean(),
      (historyEpoch, headEpoch, state, ahead) => {
        const skeleton = foldSkeleton(seedSkeleton(), [
          create(RecordSchema, {
            seq: 1n,
            epoch: historyEpoch,
            body: {
              case: 'epochStarted',
              value: {epoch: historyEpoch, runId: 'run-a'},
            },
          }),
          create(RecordSchema, {
            seq: 2n,
            epoch: historyEpoch,
            body: {
              case: 'lifecycle',
              value: {state: {case: 'running', value: {}}},
            },
          }),
        ]);
        const lifecycle = create(LifecycleSchema, {state: {case: state, value: {}}});
        const hint = {
          epoch: headEpoch,
          value: RunLiveness.ALIVE,
          head: create(RunHeadSchema, {
            epoch: headEpoch,
            committedSeq: headEpoch > historyEpoch || ahead ? 3n : 1n,
            lifecycle,
          }),
        };
        const observed = observeRun(skeleton, hint);
        const fromHead = headEpoch > historyEpoch || (headEpoch === historyEpoch && ahead);
        expect(observed.epoch).toBe(Math.max(historyEpoch, headEpoch));
        expect(observed.lifecycle?.state.case).toBe(fromHead ? state : 'running');
        expect(observeRun(skeleton).epoch).toBe(historyEpoch);
      },
    ),
    {
      numRuns: 60,
      seed: 341806,
      examples: [
        [1, 2, 'starting', true],
        [1, 1, 'parked', true],
      ],
    },
  );
});
