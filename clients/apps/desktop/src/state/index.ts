/**
 * Plain folds and views over the app's data (`neutral` runtime). Imports nothing outside this
 * folder at run time except the generated orchestrator protocol and the design system's token
 * tables (plain data), and never `effect`
 * (`desktop-state-*` rules in `.dependency-cruiser.mjs`), so a fold is a function from data to
 * data that any test calls directly.
 */
export * from './agents/index.js';
export {
  CATCH_UP_REFUSALS,
  type CatchUpPress,
  type CatchUpRefusal,
  catchUpRefusalOf,
  pressOf,
} from './catch-up.js';
export {answered, type CommandProgress, settle} from './commands.js';
export {
  type HostConnectionState,
  idleConnection,
  isTerminal,
  refusalCauseOf,
} from './connection.js';
export {
  FAULT_KINDS,
  type Fault,
  type FaultKind,
  REFUSAL_CAUSES,
  type RefusalCause,
} from './fault.js';
export type {FeedStatus} from './feed.js';
export {
  type Freshness,
  freshnessOf,
  type HostFreshness,
  hostFreshness,
  type LinkStatus,
  staleForMs,
} from './freshness.js';
export {
  type HostId,
  type HostKey,
  HostValidationError,
  hostFromKey,
  hostKey,
  hostLabel,
  validAlias,
} from './host.js';
export {
  applyHostFrame,
  beginRunsStream,
  emptyHostModel,
  type HostFrame,
  type HostModel,
  runsCursor,
} from './host-model.js';
export {
  HOST_PREREQUISITES,
  missingPrerequisites,
  PREREQUISITE_SCRIPT,
  prerequisiteMessage,
} from './host-prerequisites.js';
export {type HostRefusal, partRefusalOf, readinessOf, refusalOf} from './host-refusal.js';
export {
  HOST_STATUS_KINDS,
  type HostStatus,
  type HostStatusInput,
  type HostStatusKind,
  hostStatusOf,
  hostStatusRank,
} from './host-status.js';
export {
  type CheckInRead,
  type Facts,
  readCheckIn,
  rewind,
  type Summary,
} from './orchestrator/check-ins.js';
export {
  type CostFeedSnapshot,
  type CostModel,
  type CostRead,
  type CostUsage,
  type CostView,
  type ModelCostView,
  type RoleCostView,
  readCost,
  type SpendBucketView,
  type SpendSeriesView,
} from './orchestrator/cost.js';
export {
  FLEET_STATES,
  type FleetFilter,
  type FleetHostInput,
  type FleetHostRef,
  type FleetHostView,
  type FleetObjective,
  type FleetRow,
  type FleetState,
  type FleetView,
  fleetKey,
  fleetRows,
  fleetView,
  isLive,
  measuredObjectives,
  NO_FLEET_FILTER,
  rowObjectives,
} from './orchestrator/fleet.js';
export {
  type AgentFrame,
  applyAgentFeed,
  applyChange,
  applyCost,
  applyHostCost,
  applyRunHistory,
  applySearch,
  applySnapshot,
  applySummary,
  isFinalItem,
  type OlderFrame,
  type RunHistoryFrame,
} from './orchestrator/fold.js';
export {
  type HostCostRead,
  type HostCostView,
  type HostCostWindow,
  type HostRunCostView,
  type OtherRunsCostView,
  readHostCost,
  sinceWindow,
  windowSinceMs,
} from './orchestrator/host-cost.js';
export {type ItemOf, itemIs} from './orchestrator/items.js';
export {type MessageNote, type MessageOf, messageIs, noteOf} from './orchestrator/messages.js';
export {
  type AgentsStrip,
  type AgentTypeCount,
  agentsStrip,
  type Better,
  betterOf,
  type GainView,
  type IdeaRow,
  type IdeasReport,
  type IdeaWindow,
  ideasInRun,
  ideasInWindow,
  ideasOfVerdict,
  type MeasuredPointView,
  type ObjectiveReport,
  objectiveReports,
  type ReportHeader,
  type RunReport,
  runReport,
  type SearchReading,
  type SummaryField,
  type SummaryView,
  summaryReport,
  type TradeoffPointView,
  type TradeoffReport,
  timeTicks,
  tradeoffReport,
  type Verdict,
  valueTicks,
  type WorkstreamRow,
} from './orchestrator/report/index.js';
export {
  candidates,
  jobQueue,
  jobsWithNoRun,
  planStatus,
} from './orchestrator/search-views.js';
export {
  emptyState,
  type Gap,
  type OrchestratorState,
  type Overlay,
  type Progress,
  type RunHistoryRead,
  type SnapshotCut,
} from './orchestrator/state.js';
export {ms} from './orchestrator/time.js';
export {
  agentTurns,
  messagesOf,
  runAgents,
  unbrokenFrom,
} from './orchestrator/views.js';
export {
  type CostTarget,
  costTarget,
  initialRoute,
  type Route,
  type RouteAction,
  type RunView,
  routeReducer,
} from './route.js';
export {
  type AppearanceAction,
  type AppearanceState,
  appearanceReducer,
  defaultAppearance,
  initialAppearance,
  themeShown,
  withMode,
  withTheme,
} from './settings/appearance.js';
export {
  type MachineHost,
  type MachineRow,
  machineRow,
} from './settings/machine-rows.js';
export {
  type AddMachineEvent,
  type AddMachineState,
  addMachineStep,
  emptyAddMachine,
} from './settings/machines.js';
export {
  MATCH_LIMIT,
  matchAliases,
  parseSshConfig,
  type SshConfigEntries,
} from './settings/ssh-config.js';
export {
  parseSshCommand,
  printSshCommand,
  SshCommandError,
  type SshTarget,
  sshArgs,
  targetFromInput,
} from './settings/ssh-target.js';
export {isBack, type KeyPress} from './shell/keys.js';
export {
  type Destination,
  type FleetPlace,
  initialNavigation,
  NAVIGATION_KINDS,
  type Navigation,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
} from './shell/navigation.js';
export {
  labelFromName,
  VIEWER_ID_KEY,
  VIEWER_LABEL_KEY,
  type Viewer,
  viewerMetadata,
} from './viewer.js';
