/** Test support for the Node transport: an explicit in-memory client network. */

export {
  type ClientNetworkContractPeer,
  clientNetworkContract,
} from './client-network-contract.js';
export {
  type ClientSocketListenerKind,
  FakeClientNetwork,
  FakeClientSocket,
} from './fake-client-network.js';
