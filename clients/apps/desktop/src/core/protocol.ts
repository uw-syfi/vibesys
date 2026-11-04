/**
 * What this client says to a daemon in `Hello` and how it words a refusal. `PROTOCOL_VERSION` is
 * the revision of `OrchestratorService` this client was written against; it moves with the
 * daemon's `ProtocolVersion` (orchestrator/internal/daemon/ports.go) when a change breaks a peer.
 */
export const PROTOCOL_VERSION = 1;

/** The client's `BuildVersions`, as the initializer of that message. */
export const CLIENT_BUILD = {
  binaryVersion: 'desktop',
  protocolVersion: PROTOCOL_VERSION,
  minProtocolVersion: PROTOCOL_VERSION,
} as const;

/** The daemon reports this app is too old for it. */
export const CLIENT_TOO_OLD_TEXT =
  'this app is too old to talk to vibesys-host on this host; update the app';

/** The daemon reports it is too old for this app; `daemonVersion` is its release or `dev`. */
export const daemonTooOldText = (daemonVersion: string | undefined): string =>
  `vibesys-host on this host (${daemonVersion ?? 'unknown'}) is too old for this app; update vibesys-host there`;
