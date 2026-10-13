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
export {ConnectionError, HelperRequestError} from './shell/helper-channel.js';
export {HostConnection} from './shell/host-connection.js';
export {
  ChannelCapacityError,
  DEFAULT_SSH_POOL_CONFIG,
  type Link,
  LocalLink,
  SshLink,
} from './shell/link.js';
export type {
  ChannelCounts,
  ChannelRole,
  LinkPoolDiagnostics,
  MasterPoolDiagnostic,
} from './state/channel-admission.js';
export type {Phase} from './state/host-connection.js';
