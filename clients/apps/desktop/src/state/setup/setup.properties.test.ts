import {create} from '@bufbuild/protobuf';
import {DurationSchema} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  RunSummarySchema,
  type SetupCheckId,
  SetupCheckSchema,
  SetupCheckState,
  type SetupView,
  SetupViewSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applySummary} from '../orchestrator/fold.js';
import {emptyState} from '../orchestrator/state.js';
import {SETUP_CHECKS, setupFacts, setupOf} from './setup.js';

const stateOfCheck = fc.constantFrom(
  SetupCheckState.PENDING,
  SetupCheckState.RUNNING,
  SetupCheckState.PASSED,
  SetupCheckState.FAILED,
);

/** The checks as a host sends them: each of the six once, in id order. */
const sentChecks = fc
  .array(fc.tuple(stateOfCheck, fc.string({maxLength: 12})), {minLength: 6, maxLength: 6})
  .map(entries =>
    entries.map(([state, detail], index) =>
      create(SetupCheckSchema, {id: SETUP_CHECKS[index] as SetupCheckId, state, detail}),
    ),
  );

const finite = fc.double({noNaN: true, noDefaultInfinity: true, min: 0, max: 1e6});

const setupViews: fc.Arbitrary<SetupView> = fc
  .record({
    task: fc.constantFrom('', 'mpmc', 'tokenizer'),
    checks: sentChecks,
    baseline: fc.option(fc.tuple(finite, fc.constantFrom('ops/s', 'tok/s'), finite), {nil: null}),
    spent: finite,
    seconds: fc.integer({min: 0, max: 86_400}),
    failure: fc.constantFrom('', 'the host could not read the task'),
    unreadable: fc.boolean(),
  })
  .map(each =>
    create(SetupViewSchema, {
      task: each.task,
      checks: each.checks,
      ...(each.baseline === null
        ? {}
        : {
            baselineValue: each.baseline[0],
            baselineUnit: each.baseline[1],
            baselineSpreadPercent: each.baseline[2],
          }),
      spentUsd: each.spent,
      elapsed: create(DurationSchema, {seconds: BigInt(each.seconds)}),
      readFailure: each.failure,
      draft: each.unreadable
        ? {case: 'unreadable', value: {detail: 'no task yet'}}
        : {case: undefined},
    }),
  );

const RUNS = ['run-a', 'run-b'] as const;

const summaries = fc
  .array(
    fc.record({
      runId: fc.constantFrom(...RUNS),
      view: fc.option(setupViews, {nil: undefined}),
    }),
    {maxLength: 12},
  )
  .map(each =>
    each.map(({runId, view}) =>
      create(RunSummarySchema, {runId, ...(view === undefined ? {} : {setup: view})}),
    ),
  );

describe('the setup view fold', () => {
  it('over any sequence of summaries equals the last summary of each run', () => {
    fc.assert(
      fc.property(summaries, sequence => {
        const state = sequence.reduce(applySummary, emptyState);
        for (const runId of RUNS) {
          const last = sequence.filter(summary => summary.runId === runId).at(-1)?.setup;
          expect(setupOf(state, runId)).toEqual(last === undefined ? undefined : setupFacts(last));
        }
      }),
    );
  });

  it('is six rows in id order for any checks the host sent, a missing check pending', () => {
    fc.assert(
      fc.property(
        fc.array(fc.record({id: fc.constantFrom(...SETUP_CHECKS), state: stateOfCheck}), {
          maxLength: 10,
        }),
        sent => {
          const view = create(SetupViewSchema, {
            checks: sent.map(check => create(SetupCheckSchema, check)),
          });
          const rows = setupFacts(view).checks;
          expect(rows.map(row => row.id)).toEqual([...SETUP_CHECKS]);
          for (const row of rows)
            if (!sent.some(check => check.id === row.id)) expect(row.state).toBe('pending');
        },
      ),
    );
  });

  it('is startable exactly when all six checks passed', () => {
    fc.assert(
      fc.property(setupViews, view => {
        const passed = view.checks.every(check => check.state === SetupCheckState.PASSED);
        expect(setupFacts(view).startable).toBe(passed);
      }),
    );
  });

  it('keeps every fact in place when the host could not read, saying why', () => {
    fc.assert(
      fc.property(setupViews, view => {
        const failed = create(SetupViewSchema, {...view, readFailure: 'cannot read'});
        const without = create(SetupViewSchema, {...view, readFailure: ''});
        const read = setupFacts(failed);
        expect(read.readFailure).toBe('cannot read');
        expect({...read, readFailure: null}).toEqual(setupFacts(without));
      }),
    );
  });
});
