import {expect, test} from 'bun:test';
import {RunLiveness, RunLivenessSchema} from '@vibesys/proto';
import fc from 'fast-check';
import {aliveRuns, type RunFacts} from './index.js';

/** Every liveness the contract defines, read from its generated schema and not listed by hand. */
const LIVENESS: readonly RunLiveness[] = RunLivenessSchema.values.map(value => value.number);

function runWith(liveness: RunLiveness): RunFacts {
  return {projectRoot: undefined, head: undefined, liveness};
}

test('the contract defines more than alive and ended, so the test below means something', () => {
  expect(LIVENESS.length).toBeGreaterThan(2);
  expect(LIVENESS).toContain(RunLiveness.ALIVE);
  expect(LIVENESS).toContain(RunLiveness.ENDED);
});

test('property: of any runs, those alive are exactly those whose liveness is ALIVE, whatever the others are', () => {
  fc.assert(
    fc.property(fc.array(fc.constantFrom(...LIVENESS), {maxLength: 60}), levels => {
      const runs = new Map(levels.map((liveness, index) => [`run-${index}`, runWith(liveness)]));
      expect(aliveRuns(runs)).toBe(levels.filter(each => each === RunLiveness.ALIVE).length);
    }),
  );
});

test('a run in any state but ALIVE is never counted alone', () => {
  for (const liveness of LIVENESS) {
    const counted = aliveRuns(new Map([['run', runWith(liveness)]]));
    expect(counted).toBe(liveness === RunLiveness.ALIVE ? 1 : 0);
  }
});
