/** Realistic orchestrated runs as event streams, and a fake daemon that serves them. */
export {busyFacts} from './check-ins.js';
export {
  ELIDE_OVER,
  FAKE_VIEWER,
  FakeDaemon,
  NO_VIEWER_REFUSAL,
} from './fake-daemon.js';
export {fleetScenario} from './fleet.js';
export {faultsOn, type HostHarness, hostHarness} from './harness.js';
export {
  type DaemonOptions,
  type DispatchFate,
  daemonRecording,
  methodPathOf,
} from './recordings.js';
export {
  type MeasuredOptions,
  measuredSearch,
  type ObjectiveSpec,
  SERVING,
  withReport,
} from './report-fixtures.js';
export {generateScenario, START_MS} from './scenario.js';
export {serveSummaries} from './served-summary.js';
