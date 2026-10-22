/**
 * Test support for the connection manager's consumers: an in-memory host helper (`FakeHelper`)
 * behind a `FakeLink`, live-run records, a fake child process, and `phaseReached` for waiting on a
 * connection's phase.
 *
 * The `HostConnection` contract suite every implementation passes (`host-connection-contract.ts`)
 * is deliberately not re-exported here: it runs under `bun:test`, and a plain-Node importer of this
 * barrel (an Electron main-process E2E harness included) must not have that import forced onto it.
 * Its one consumer, this package's own `host-connection.test.ts`, imports it by its relative path.
 */

export {FakeGateway, type FakeTail} from './fake-gateway.js';
export {
  ECHO_SERVER,
  FAKE_HARNESSES,
  FAKE_MODEL_CATALOGUE,
  FakeHelper,
  FakeLink,
  record,
} from './fake-helper.js';
export {FakeLogStore, type LogStoreFixture} from './fake-log-store.js';
export {deferredKillProcess, fakeProcess, pipeHeldProcess} from './fake-process.js';
export {commandOf, olderHost} from './older-host.js';
export {phaseReached} from './phase-reached.js';
