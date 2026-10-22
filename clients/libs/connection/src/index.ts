/**
 * The desktop's one way to reach a host (`docs/contributing/desktop-connection.md`, "Connection
 * manager"): `ConnectionManager` keeps one `HostConnection` per host over a `Link` (This Mac, or an
 * ssh master), whose channels speak the host helper's typed protocol. `state/` is the pure machine;
 * `shell/` owns every process, channel, and timer. `connectionStatus` words a connection's phase
 * for the views. Fakes for consumers' tests, and the `HostConnection` contract suite, are in
 * `./testing`.
 */

export {connectionStatus} from './connection-status.js';
export {controlDirectory, installAskpass} from './shell/askpass.js';
export {
  type ConnectionLease,
  ConnectionManager,
  DEFAULT_CONNECTION_CONFIG,
} from './shell/connection-manager.js';
export {
  GatewayConnection,
  type GatewayDialer,
  HELLO_METHOD,
  READ_METHOD,
  TAIL_METHOD,
} from './shell/gateway.js';
export {GatewayCalls} from './shell/gateway-calls.js';
export {localGateway} from './shell/gateway-local.js';
export {
  GATEWAY_DIRECTORY,
  type GatewayTarget,
  gatewayArtifact,
  gatewayProcess,
  gatewayTarget,
  provisionGateway,
} from './shell/gateway-provision.js';
export {GrpcDecoder, GrpcError, grpcFrame} from './shell/grpc.js';
export {ConnectionError, HelperRequestError} from './shell/helper-channel.js';
export {HostConnection, type LogsLease} from './shell/host-connection.js';
export {type Link, LocalLink, SshLink} from './shell/link.js';
export {fromChild, type SpawnedProcess} from './shell/process.js';
export {
  type RegistryCatalog,
  type RegistryReadPorts,
  registryReads,
  registryTransport,
  requireCheckout,
} from './shell/registry-reads.js';
export {
  type CompletedPaths,
  type InspectedCandidate,
  type RegistrySetup,
  type RegistrySetupPorts,
  registrySetup,
} from './shell/registry-setup.js';
export type {CheckoutProblem, Phase} from './state/host-connection.js';
