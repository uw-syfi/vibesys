/**
 * The domain models the views and the main process share, and the pure selectors over them: hosts
 * and their connection status, run identity and phase, a host's listed runs, recent runs, the stop
 * flow, and task arguments. Values and pure functions only: no I/O, no runtime library.
 */
export type {ConnectionStatus} from './connection-status.js';
export {
  type HostId,
  type HostKey,
  type HostSummary,
  hostFromKey,
  hostKey,
  hostLabel,
  SettingsError,
  SSH_ALIAS,
  validAlias,
  validHostPath,
} from './host.js';
export {isInstanceId, isRunId, isTaskName} from './ids.js';
export {type HostListing, type HostRun, hostListing, liveRecord, phaseText} from './listing.js';
export {
  type RecentRun,
  type RecentStatus,
  recentStatus,
  suggestProjects,
} from './recent-status.js';
export {
  exactResumeRunId,
  type ResumeDecision,
  ResumeRequestError,
  type ResumeRunId,
  RUN_ID_UNAVAILABLE,
  resumeArguments,
  resumeDecision,
} from './resume-run.js';
export {ShellWordsError, shellWords} from './shell-words.js';
export {
  confirmText,
  STOP_IDLE,
  type StopEvent,
  type StopState,
  type StopStep,
  type StopView,
  stopKey,
  stopStep,
  stopView,
} from './stop-run.js';
