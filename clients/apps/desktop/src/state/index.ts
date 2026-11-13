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
  completionsOf,
  type FolderEntry,
  type FolderListing,
  listingKey,
  type PathCompletion,
  type PathSplit,
  splitPath,
} from './directory-listing.js';
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
  DORMANT_GENERIC_RUN_INDEX,
  type GenericRunActivity,
  type GenericRunEntry,
  type GenericRunIndex,
  type GenericRunIndexUnavailable,
  type GenericRunObjective,
  type GenericRunState,
} from './generic-run-index.js';
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
  withRunSearchResult,
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
  type AbsenceCause,
  type GpuOverview,
  type GpuReading,
  gpuOverview,
  type MachineSample,
  type MachineView,
  type MachineWhy,
  type MemoryUse,
  machineView,
  type Section,
} from './machine-stats.js';
export * from './new-run/index.js';
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
  type ExploredWorkstreams,
  exploredWorkstreams,
} from './orchestrator/cost-workstreams.js';
export {
  FLEET_SEARCH_REGISTRY,
  FLEET_STATES,
  type FleetAbsence,
  type FleetAttention,
  type FleetDiscoveryStatus,
  type FleetFilter,
  type FleetHostInput,
  type FleetHostRef,
  type FleetHostView,
  type FleetObjective,
  type FleetRemoteSearch,
  type FleetRow,
  type FleetSearchEntry,
  type FleetSearchField,
  type FleetState,
  type FleetUpdate,
  type FleetView,
  fleetKey,
  fleetRows,
  fleetSearchScore,
  fleetView,
  type GenericDiscoveryStatus,
  type GenericFleetRow,
  isLive,
  type ManagedFleetRow,
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
  replaceSnapshot,
  type WholeViewPhase,
} from './orchestrator/fold.js';
export {
  COST_DAY_MS,
  type HostCostRead,
  type HostCostView,
  type HostCostWindow,
  type HostRunCostView,
  lastDayWindow,
  type OtherRunsCostView,
  readHostCost,
  sinceWindow,
  windowSinceMs,
} from './orchestrator/host-cost.js';
export {
  bucketsInRange,
  HOST_ORDER,
  nextRunSort,
  type RunSort,
  type RunSortKey,
  rangeOf,
  rangesOf,
  type SpendRange,
  sortRuns,
  spendSpanMs,
} from './orchestrator/host-cost-lists.js';
export {type ItemOf, itemIs} from './orchestrator/items.js';
export {type MessageNote, type MessageOf, messageIs, noteOf} from './orchestrator/messages.js';
export {
  type AgentCostRow,
  type AgentsStrip,
  type AgentTypeCount,
  agentCostRows,
  agentsStrip,
  type BandView,
  type Better,
  bandView,
  betterOf,
  boundIsDrawn,
  choiceOf,
  type GainView,
  historyStart,
  type IdeaRow,
  type IdeaSortKey,
  type IdeasReport,
  type IdeaWindow,
  ideasInRun,
  ideasInWindow,
  ideasMatching,
  ideasOfVerdict,
  LATEST_COUNT,
  type LatestMeasurement,
  type LatestValue,
  type MeasuredPointView,
  nextSort,
  type ObjectiveReport,
  objectiveReports,
  offersRanges,
  type RangeChoice,
  type RangeView,
  type ReportHeader,
  type RunFacts,
  type RunReport,
  rangeChoices,
  runReport,
  type SearchReading,
  type SortDirection,
  type Span,
  type SummaryField,
  type SummaryView,
  sortIdeas,
  summaryReport,
  type TradeoffPointView,
  type TradeoffReport,
  tradeoffReport,
  VERDICT_ORDER,
  type Verdict,
  valueTicks,
  verdictCounts,
  WHOLE_RUN,
  type WorkstreamRow,
  windowOf,
} from './orchestrator/report/index.js';
export {
  type Change as SettingChangeView,
  type CostSetting,
  changeOf as settingChangeOf,
  type FanoutSetting,
  type LastWrite,
  type LeadSetting,
  type LeadState,
  lastWrite,
  type RunSettings,
  runSettingsOf,
  settingsHistory,
  settingsTimeline,
  type TimelineRow,
  type TimeSetting,
} from './orchestrator/run-settings.js';
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
  type SettingsRecord,
  type SnapshotCut,
} from './orchestrator/state.js';
export {ms} from './orchestrator/time.js';
export {
  agentTurns,
  holdsSteps,
  messagesOf,
  runAgents,
  unbrokenFrom,
} from './orchestrator/views.js';
export {workingByRole} from './orchestrator/working-by-role.js';
export {
  type RepositoryAnswer,
  type RepositoryChecked,
  type RepositoryVerdict,
  repositoryVerdict,
} from './repository-check.js';
export {
  type CostTarget,
  costTarget,
  initialRoute,
  type Route,
  type RouteAction,
  type RunView,
  routeReducer,
} from './route.js';
export {isManagedRun} from './run-source.js';
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
export {type Reach, reachOf, unreadableDetail} from './settings/eval-places.js';
export {
  emptyMachineDraft,
  type MachineConnectionKind,
  type MachineDraft,
  type MachineDraftResult,
  machineDraftResult,
} from './settings/machine-draft.js';
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
  type MachineEntry,
  type MachineSources,
  type MachinesFacts,
  type MachineTableRow,
  machineEntries,
  machineKey,
  machinesFacts,
  machineTableRows,
  type PlaceAsk,
  type PlaceNote,
  placesAsked,
  settleAsks,
} from './settings/machines-page.js';
export {
  checkRepositoriesFolder,
  type FolderCheck,
  REPOSITORIES_FIELD,
} from './settings/repositories-folder.js';
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
export {
  type Baseline,
  type CheckRow,
  type CheckState,
  type Draft,
  followedAgent,
  inSetup,
  SETUP_CHECKS,
  type SetupFacts,
  type StartOutcome,
  setupFacts,
  setupLead,
  setupOf,
  startOutcome,
  UNREAD_SETUP,
  unmetOf,
} from './setup/index.js';
export {isBack, type KeyPress} from './shell/keys.js';
export {
  type Destination,
  type FleetPlace,
  initialNavigation,
  type MachinesPlace,
  NAVIGATION_KINDS,
  type Navigation,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
  type RunReceipt,
  runIsUnread,
} from './shell/navigation.js';
export {
  labelFromName,
  VIEWER_ID_KEY,
  VIEWER_LABEL_KEY,
  type Viewer,
  viewerMetadata,
} from './viewer.js';
