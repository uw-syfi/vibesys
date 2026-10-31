/** The generated runs of the tests of the one reading of an objective's best (`generated-runs.ts`). */
import fc from 'fast-check';
import type {Run} from './generated-runs.js';

/** Runs of every shape: either direction, sessions that measured the input again, ideas out of order. */
export const run: fc.Arbitrary<Run> = fc
  .record({
    direction: fc.constantFrom('max', 'min'),
    input: fc.integer({min: 10, max: 5000}),
    sessionInputs: fc.array(fc.integer({min: 10, max: 5000}), {minLength: 1, maxLength: 3}),
    ideas: fc.array(
      fc.record({
        value: fc.integer({min: 1, max: 10000}),
        at: fc.integer({min: 1, max: 600}),
        passed: fc.boolean(),
        session: fc.nat(2),
      }),
      {minLength: 0, maxLength: 16},
    ),
    cursor: fc.double({min: 0, max: 1, noNaN: true}),
  })
  .map(each => ({
    ...each,
    // The first session's re-measure is the run's input.
    sessionInputs: [each.input, ...each.sessionInputs.slice(1)],
  }));
