import {getOption} from '@bufbuild/protobuf';
import {
  file_vibesys_orchestrator_v1_service,
  min_protocol_version,
  protocol_version,
} from '@vibesys/orchestrator-proto';

/**
 * What this client says to a daemon in `Hello` and how it words a refusal. `PROTOCOL_VERSION` is
 * the revision of `OrchestratorService` this client was written against. It is read from the
 * service's file options in `service.proto`, the one place the daemon and the Go client read it
 * too, so a change that breaks a peer moves all of them together.
 */
export const PROTOCOL_VERSION = getOption(file_vibesys_orchestrator_v1_service, protocol_version);

/** The oldest revision the app still speaks, from the same options. */
const MIN_PROTOCOL_VERSION = getOption(
  file_vibesys_orchestrator_v1_service,
  min_protocol_version,
);

/** The client's `BuildVersions`, as the initializer of that message. */
export const CLIENT_BUILD = {
  binaryVersion: 'desktop',
  protocolVersion: PROTOCOL_VERSION,
  minProtocolVersion: MIN_PROTOCOL_VERSION,
} as const;

/** The daemon reports this app is too old for it. */
export const CLIENT_TOO_OLD_TEXT =
  'this app is too old to talk to vibesys-host on this host; update the app';

/** The daemon reports it is too old for this app; `daemonVersion` is its release or `dev`. */
export const daemonTooOldText = (daemonVersion: string | undefined): string =>
  `vibesys-host on this host (${daemonVersion ?? 'unknown'}) is too old for this app; update vibesys-host there`;
