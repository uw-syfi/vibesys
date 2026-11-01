import {expect, test} from '@playwright/test';
import {recordedMatches, staleFailures, suitesWithFailures} from './checks/known-ledger.js';

/**
 * A fixed defect leaves the list of known failures (`checks/known-failures.ts`): an entry that no
 * screen of its suite showed in this run is stale. The screen specs run on any worker in any order,
 * so this runs as the config's `ledger` project, which starts after every screen test has passed
 * and reads what their ledgers recorded (`checks/known-ledger.ts`). A suite whose spec is in
 * another slice of the run has recorded nothing, and is skipped here and checked there. It is not
 * named `*.spec.ts`, so the plan that shares the specs among the CI jobs does not place it in one:
 * every job runs it.
 */
for (const [suite, failures] of suitesWithFailures()) {
  test(`every known failure of the ${suite} screens still happens, so a fixed defect leaves the list`, () => {
    const matched = recordedMatches(suite);
    test.skip(matched === null, `no screen of the ${suite} suite was walked in this run`);
    const stale = staleFailures(failures, matched ?? new Set());
    expect(
      stale,
      `known failures no screen shows any more, delete them: ${stale.join(', ')}`,
    ).toEqual([]);
  });
}
