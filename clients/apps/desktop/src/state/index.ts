/**
 * Plain folds and views over the app's data (`neutral` runtime). Imports nothing outside this
 * folder at run time except the generated orchestrator protocol, and never `effect`
 * (`desktop-state-*` rules in `.dependency-cruiser.mjs`), so a fold is a function from data to
 * data that any test calls directly.
 */
export {answered, type CommandProgress, settle} from './commands.js';
export {
  type HostConnectionState,
  idleConnection,
  isTerminal,
} from './connection.js';
export {FAULT_KINDS, type Fault, type FaultKind} from './fault.js';
export type {FeedStatus} from './feed.js';
export {type Freshness, freshnessOf} from './freshness.js';
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
  emptyHostModel,
  type HostFrame,
  type HostModel,
  runsCursor,
} from './host-model.js';
export {
  type AgentFrame,
  applyAgentFeed,
  applyEvent,
  applySnapshot,
  isFinalItem,
  type OlderFrame,
} from './orchestrator/fold.js';
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
  type UsageReport,
  type UsageTotals,
  unbrokenFrom,
  usageOf,
} from './orchestrator/views.js';
export {
  initialRoute,
  type Route,
  type RouteAction,
  type RunView,
  routeReducer,
} from './route.js';
