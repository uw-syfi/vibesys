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
  pressOf,
  refusalOf,
} from './catch-up.js';
export {answered, type CommandProgress, settle} from './commands.js';
export {
  type HostConnectionState,
  idleConnection,
  isTerminal,
} from './connection.js';
export {FAULT_KINDS, type Fault, type FaultKind} from './fault.js';
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
} from './orchestrator/cost.js';
export {
  type AgentFrame,
  applyAgentFeed,
  applyChange,
  applyCost,
  applySearch,
  applySnapshot,
  isFinalItem,
  type OlderFrame,
} from './orchestrator/fold.js';
export {type ItemOf, itemIs} from './orchestrator/items.js';
export {type MessageNote, type MessageOf, messageIs, noteOf} from './orchestrator/messages.js';
export {
  candidates,
  jobQueue,
  jobsWithNoRun,
  planStatus,
  resultSoFar,
} from './orchestrator/search-views.js';
export {
  emptyState,
  type Gap,
  type OrchestratorState,
  type Overlay,
  type Progress,
} from './orchestrator/state.js';
export {ms} from './orchestrator/time.js';
export {
  agentThread,
  agentTree,
  agentTurns,
  type Bar,
  type FanoutPoint,
  fanoutOverTime,
  flattenTree,
  type JobBar,
  messagesOf,
  runAgents,
  type ThreadItem,
  type ThreadTurn,
  type Timeline,
  type TimelineRow,
  type TreeNode,
  timeline,
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
  type ResourceConnection,
  type ResourceHost,
  type ResourceRow,
  resourceRow,
} from './settings/resources.js';
export {SETTINGS_TABS, type SettingsTab} from './settings/tabs.js';
export {isBack, type KeyPress} from './shell/keys.js';
export {
  type Destination,
  initialNavigation,
  NAVIGATION_KINDS,
  type Navigation,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
} from './shell/navigation.js';
