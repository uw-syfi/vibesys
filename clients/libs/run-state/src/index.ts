/**
 * `@vibesys/run-state`: the run event fold. Backend snapshots and ordered run events fold into one
 * read-only `CoreState`; reducers return new state and perform no I/O. Round, performance, and
 * transcript projections over that state are separate entries (`./rounds`, `./performance`,
 * `./transcript`). Replay and merge machinery stays package-private.
 */

export {latestDiagnosticChange} from './diagnostics.js';
export {type ExecutionStatus, executionStatusFor} from './execution-status.js';
export {reconcileActiveExecutions} from './executions.js';
export {
  type IterationNavigationState,
  initialIterationNavigation,
  iterationChildren,
  iterationRoots,
  iterationRootsForCore,
  reduceIterationResult,
} from './iteration-navigation.js';
export {
  type ActiveExecutionCheckpointFold,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceResponseEvents,
  reduceSnapshot,
} from './reduce.js';
export {
  type ActiveAgentExecution,
  type ActiveExecutionCheckpoint,
  type AgentExecutionMode,
  type CoreDiagnostic,
  type CoreRunStatus,
  type CoreState,
  type EndedRunStatus,
  type ExecutionTodos,
  hasRunEnded,
  initialCoreState,
  type QuotaPause,
  type QuotaState,
  type RunLifetimeBoundary,
  type RunStartupState,
  rehydrateCoreState,
  type TodoItem,
  type UsageMeter,
} from './state.js';
