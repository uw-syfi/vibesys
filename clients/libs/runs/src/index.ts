/**
 * `@vibesys/runs`: what the app knows about a run as data: its identifiers, the status a recent run
 * shows from its host's listing, launch intent, the exact-resume policy, and the stop flow's pure
 * step. No I/O.
 */
export {isInstanceId, isRunId, isTaskName} from './ids.js';
export {
  applyLaunchProfileMutation,
  canonicalLaunchArguments,
  LAUNCH_ARGUMENT_BYTE_LIMIT,
  LAUNCH_ARGUMENT_COUNT_LIMIT,
  LAUNCH_ARGUMENT_TOTAL_BYTE_LIMIT,
  LAUNCH_PROFILE_LABEL_BYTE_LIMIT,
  LAUNCH_PROFILE_LABEL_CODE_POINT_LIMIT,
  LAUNCH_PROFILE_LIMIT,
  type LaunchArguments,
  type LaunchProfile,
  LaunchProfileConflictError,
  type LaunchProfileDraft,
  type LaunchProfileId,
  type LaunchProfileMutation,
  LaunchProfileValidationError,
  launchArguments,
  launchProfileId,
  parseLaunchProfile,
  parseLaunchProfileDraft,
  parseLaunchProfiles,
} from './launch-profile.js';
export {
  knownProjects,
  type RecentRun,
  type RecentStatus,
  recentRunKey,
  recentStatus,
  type SkippedRecent,
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
export {parseRunRef, type RunRef, runRefKey, sameRun} from './run-ref.js';
export {
  type HarnessPick,
  type MeasureTarget,
  parseRunStart,
  parseStoredRunStart,
  type RoleOption,
  type RunOptions,
  type RunStart,
} from './run-start.js';
export {canonicalShellWords, ShellWordsError, shellWords} from './shell-words.js';
