/** Realistic orchestrated runs as event streams, and a fake daemon that serves them. */
export {FakeDaemon} from './fake-daemon.js';
export {type FleetOptions, fleetScenario} from './fleet.js';
export {faultsOn, type HostHarness, hostHarness} from './harness.js';
export {
  type DaemonOptions,
  type DispatchFate,
  daemonRecording,
  methodPathOf,
} from './recordings.js';
export {generateScenario} from './scenario.js';
