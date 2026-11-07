/** Realistic orchestrated runs as event streams, and a fake daemon that serves them. */
export {ELIDE_OVER, FakeDaemon} from './fake-daemon.js';
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
export {generateScenario} from './scenario.js';
