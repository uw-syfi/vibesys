/**
 * `@vibesys/hosts`: the machines the app runs on, as pure, validated data shared by the main
 * process and the views: a host's identity and key, its label, the checkout and project path rules,
 * its registry listing, and what the window shows about its connection. No I/O.
 */

export type {InstanceInspection} from '@vibesys/contracts/host-helper';
export type {ConnectionStatus} from './connection-status.js';
export {
  type HostId,
  type HostKey,
  type HostSummary,
  HostValidationError,
  hostFromKey,
  hostKey,
  hostLabel,
  SSH_ALIAS,
  validAlias,
  validHostPath,
} from './host.js';
export {inspectionReply} from './inspection.js';
export {
  type HostListing,
  type HostRun,
  hostListing,
  type InstanceObservation,
  instanceObservation,
  instancePhaseText,
  type RunEnd,
  type RunPause,
} from './listing.js';
