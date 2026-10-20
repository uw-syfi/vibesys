/**
 * `@vibesys/hosts`: the machines the app runs on, as pure, validated data shared by the main
 * process and the views: a host's identity and key, its label, the checkout and project path rules,
 * path completion for those paths, its registry listing, and what the window shows about its
 * connection. No I/O.
 */

export type {
  CheckoutInspectReply,
  InstanceInspection,
  MachineDescription,
} from '@vibesys/contracts/host-helper';
export {acceleratorName} from './accelerator.js';
export {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionField,
  type CompletionRequest,
  type CompletionResult,
  type CompletionState,
  completionStep,
  type PathCompletion,
  splitPath,
  suggestions,
} from './completion.js';
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
  newestServerOf,
  type RunEnd,
  type RunPause,
} from './listing.js';
export {
  connectionProblem,
  MACHINE_PROBLEMS,
  type MachineProblem,
  type MachineStatus,
  mostUrgentProblem,
  problemWord,
} from './machine-status.js';
export {missingRequiredFeatures, REQUIRED_HELPER_FEATURES} from './required-features.js';
export {
  digestEntries,
  digestLook,
  type IdeaPoint,
  type ListedRun,
  listedRuns,
  RUN_GONE_WORDS,
  type RunDashboardEntry,
  type RunDigestEntry,
  type RunDigestsReply,
} from './run-digest.js';
export {type EndKind, type EndLook, type EndTone, endedLook} from './run-look.js';
export {
  type AgentSpend,
  type RunSpend,
  USAGE_WINDOW_MAX_HOURS,
  USAGE_WINDOW_MAX_RUNS,
  type UsageWindowEntry,
  type UsageWindowOutcome,
  usageWindowEntry,
  usageWindowRefusal,
} from './usage-window.js';
