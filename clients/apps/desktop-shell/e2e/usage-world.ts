/** The Usage worlds (`src/testing/usage-world.ts`), and the run a listed row opens over the native gateway. */
import {journeyFixture} from './journey-world.js';

export {seedFor, USAGE_CASES, type UsageCase} from '../src/testing/usage-world.js';

import {newestRunId, type UsageCase} from '../src/testing/usage-world.js';

/** The listed run reached from Usage uses the same native gateway as every run journey. */
export function runFixtureFor(usage: UsageCase) {
  const fixture = journeyFixture('empty');
  return {...fixture, runId: newestRunId(usage) ?? fixture.runId};
}
