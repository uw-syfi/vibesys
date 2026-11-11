/** Realistic orchestrated runs as event streams, and a fake daemon that serves them. */
export {busyFacts} from './check-ins.js';
export {
  ELIDE_OVER,
  FAKE_VIEWER,
  FakeDaemon,
  NO_VIEWER_REFUSAL,
} from './fake-daemon.js';
export {FIXTURE_FILES} from './fake-files.js';
export type {SetupFlavor} from './fake-setup.js';
export {fleetScenario} from './fleet.js';
export {faultsOn, type HostHarness, hostHarness} from './harness.js';
export {
  type DaemonOptions,
  type DispatchFate,
  daemonRecording,
  methodPathOf,
} from './recordings.js';
export {
  REPORT_CASES,
  type ReportCase,
  reportCaseWorld,
} from './report-cases.js';
export {
  type MeasuredOptions,
  measuredSearch,
  type ObjectiveSpec,
  SERIES_LIMIT,
  SERVING,
  withReport,
} from './report-fixtures.js';
export {generateScenario, START_MS} from './scenario.js';
export {serveSummaries} from './served-summary.js';
export {setupDaemon} from './setup-daemon.js';
