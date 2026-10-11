/**
 * Test support for the connection manager's consumers: an in-memory host helper (`FakeHelper`)
 * behind a `FakeLink`, live-run records, a fake child process, and the `HostConnection` contract
 * suite every implementation passes.
 */
export {ECHO_SERVER, FakeHelper, FakeLink, record} from './fake-helper.js';
export {deferredKillProcess, fakeProcess} from './fake-process.js';
export {hostConnectionContract, phaseReached} from './host-connection-contract.js';
