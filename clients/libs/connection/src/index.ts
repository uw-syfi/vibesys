/**
 * The desktop's one way to reach a host (`docs/contributing/desktop-connection.md`, "Connection
 * manager"): `ConnectionManager` keeps one `HostConnection` per host over a `Link` (This Mac, or an
 * ssh master), whose channels speak the host helper's typed protocol. `state/` is the pure machine;
 * `shell/` owns every process, channel, and timer. `connectionStatus` words a connection's phase
 * for the views. Fakes for consumers' tests, and the `HostConnection` contract suite, are in
 * `./testing`.
 */

export {connectionStatus} from './connection-status.js';
export {
  hostListingFromRuns,
  LISTING_FAILURE_WORDS,
  listingStep,
  watchedListing,
} from './run-listing.js';
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
  MUTATE_METHOD,
  READ_METHOD,
  TAIL_METHOD,
  WATCH_METHOD,
} from './shell/gateway.js';
export {GatewayCalls} from './shell/gateway-calls.js';
export {localGateway} from './shell/gateway-local.js';
export {
  GATEWAY_DIRECTORY,
  GATEWAY_STDERR_BYTES,
  type GatewayTarget,
  gatewayArtifact,
  gatewayProcess,
  gatewayTarget,
  provisionGateway,
} from './shell/gateway-provision.js';
export {CallTimedOut, GrpcDecoder, grpcFrame} from './shell/grpc.js';
export {ConnectionError, HelperRequestError} from './shell/helper-channel.js';
export {HostConnection, type LogsLease} from './shell/host-connection.js';
export {type HostRuns, type HostRunsSource, hostRuns} from './shell/host-runs.js';
export {HOST_UNSUPPORTED_MESSAGE, isHostUnsupported} from './shell/host-unsupported.js';
export {type Link, LocalLink, SshLink} from './shell/link.js';
export {LoginActor, type LoginPorts, type LoginStart} from './shell/login-actor.js';
export {fromChild, type SpawnedProcess} from './shell/process.js';
export {type RegistryRunCatalog, registryRunCatalog} from './shell/registry-catalog.js';
export {
  type RegistryClusterPorts,
  type RegistryClusters,
  registryClusters,
} from './shell/registry-clusters.js';
export {
  type RegistryProjectPorts,
  type RegistryProjects,
  registryProjects,
} from './shell/registry-projects.js';
export {
  type RegistryCatalog,
  type RegistryReadPorts,
  registryReads,
  registryTransport,
  requireCheckout,
} from './shell/registry-reads.js';
export {
  digestEntries,
  type RegistryRunReads,
  registryRunReads,
} from './shell/registry-run-reads.js';
export {
  type RegistrySavedRuns,
  registrySavedRuns,
  SavedRunsInterrupted,
  type SavedRunsPorts,
} from './shell/registry-saved-runs.js';
export {
  type CompletedPaths,
  type InspectedCandidate,
  type RegistrySetup,
  type RegistrySetupPorts,
  registrySetup,
} from './shell/registry-setup.js';
export {
  type RegistryRunInputs,
  type RegistryStartPorts,
  registryStart,
} from './shell/registry-start.js';
export {
  type RegistryUsage,
  type RegistryUsagePorts,
  registryUsage,
  usageSummaryEntry,
} from './shell/registry-usage.js';
export {
  SUBSCRIPTION_LINGER_MS,
  SubscriptionFailed,
  type SubscriptionStreams,
  Subscriptions,
} from './shell/subscriptions.js';
export {GATEWAY_FIRST_CONNECT_BUDGET_MS} from './state/gateway.js';
export type {CheckoutProblem, Phase} from './state/host-connection.js';
