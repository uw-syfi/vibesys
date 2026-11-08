/** A fake daemon holding one run in its setup phase, with the host's scripted setup world attached. */
import {FakeDaemon} from './fake-daemon.js';
import {FakeSetup, type SetupFlavor, setupScenario} from './fake-setup.js';

/** The daemon, and the setup world a test reaches the host's side of setup through (`restLead`). */
export function setupDaemon(flavor: SetupFlavor): {
  readonly daemon: FakeDaemon;
  readonly world: FakeSetup;
} {
  const scenario = setupScenario();
  const daemon = new FakeDaemon(scenario);
  const world = new FakeSetup(daemon.ledger(), flavor);
  daemon.setup = world;
  world.adopt(scenario.runId);
  return {daemon, world};
}
