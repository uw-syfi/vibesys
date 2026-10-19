/**
 * `@vibesys/run-fixtures`: replayable run fixtures for client harnesses and checks. A fixture is a
 * run's events plus how to play them back (`RunFixture`); they come from a recording of a real run
 * (`recordFixture`, with every host, path and user name scrubbed) or from a synthetic scenario
 * (`scenarios`, `weeksRun`). `catalog` is the committed set, by name.
 */

export {agentLogins, loginStarts} from './agent-logins.js';
export {
  committedFixtures,
  FIXTURE_NAMES,
  fixtureNamed,
} from './catalog.js';
export {
  clusterTestPassed,
  clusterTestPassedUntyped,
  clusterTestsFailed,
  computeSettings,
  computeSettingsEmpty,
  loginNodeCluster,
  measurementChecks,
  operatorCluster,
  sshCluster,
} from './compute.js';
export {exampleCatalogue, exampleCatalogueWithoutDetails, queueRsCatalogue} from './examples.js';
export {
  END_OUTCOMES,
  type EndOutcome,
  FIXTURE_FORMAT,
  type FixtureObjective,
  parseFixture,
  type RunFixture,
  splitLive,
} from './fixture.js';
export {
  clusterTestsByAlias,
  loginStartsByProvider,
  type MachineSettingsFixture,
  machineSettingsWorld,
} from './machine-settings.js';
export {journalEvents, type RecordRequest, recordFixture} from './record.js';
export {FAILURE_REASONS, scenarios} from './scenarios.js';
export {
  findLeaks,
  type Leak,
  type LeakKind,
  NO_NAMES,
  type ScrubPolicy,
  scrubText,
  scrubValue,
} from './scrub.js';
export {weeksRun} from './weeks.js';
