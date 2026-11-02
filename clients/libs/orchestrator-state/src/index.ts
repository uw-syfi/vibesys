/**
 * The orchestrator's client state: the folds that turn the daemon's snapshot and events into one
 * state, and the reads the screens take of it. Pure; a connection feeds it (docs/contributing/
 * clients-architecture.md, "State").
 */
export {type AgentFeed, applyAgentFeed, applyEvent, applySnapshot, isFinalItem} from './fold.js';
export {emptyState, type Gap, type OrchestratorState, type Overlay} from './state.js';
export {ms} from './time.js';
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
  oldestOrdinal,
  runAgents,
  type ThreadItem,
  type ThreadTurn,
  type Timeline,
  type TimelineRow,
  type TreeNode,
  timeline,
  type UsageReport,
  type UsageTotals,
  usageOf,
} from './views.js';
