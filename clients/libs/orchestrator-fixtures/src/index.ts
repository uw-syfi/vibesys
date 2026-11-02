/**
 * Realistic orchestrated runs as event streams, a fake daemon that serves them, and the fixture
 * source the screens read from before the real daemon exists.
 */
export {FakeDaemon} from './fake-daemon.js';
export {FixtureSource, type FixtureSourceOptions} from './fixture-source.js';
export {loopbackLauncher} from './loopback.js';
export {generateScenario, type Scenario, type ScenarioOptions} from './scenario.js';
