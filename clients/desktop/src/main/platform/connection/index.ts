/**
 * The desktop's one way to reach a host (`docs/contributing/desktop-connection.md`, "Connection
 * manager"): `ConnectionManager` keeps one `HostConnection` per host over a `Link` (This Mac, or an
 * ssh master), whose channels speak the host helper's typed protocol. `state/` is the pure machine;
 * `shell/` owns every process, channel, and timer.
 */

export {controlDirectory, installAskpass} from './shell/askpass.js';
export {
  ConnectionManager,
  DEFAULT_CONNECTION_CONFIG,
} from './shell/connection-manager.js';
export {ConnectionError, HelperRequestError} from './shell/helper-channel.js';
export {HostConnection} from './shell/host-connection.js';
export {type Link, LocalLink, SshLink} from './shell/link.js';
export type {Phase} from './state/host-connection.js';
