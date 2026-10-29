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
  type EventFold,
  foldEventBatch,
  foldEventRebootstrap,
  foldResponseEvents,
  foldSnapshot,
  type RunIdentityRejection,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceResponseEvents,
  reduceSnapshot,
  type SnapshotFold,
  type SnapshotFoldDisposition,
} from './reduce.js';
export {
  type ActiveAgentExecution,
  type ActiveExecutionCheckpoint,
  type AgentExecutionMode,
  type ChatProposal,
  type ChatQuestion,
  type ChatThread,
  type ComparisonBaseline,
  type CoreDiagnostic,
  type CoreRunStatus,
  type CoreState,
  type EndedRunStatus,
  type ExecutionTodos,
  hasRunEnded,
  type InputMeasurementFailure,
  initialCoreState,
  type MeasurementComparison,
  type ProviderSwitch,
  type QuotaPause,
  type QuotaState,
  type QuotaStop,
  type RunLifetimeBoundary,
  type RunStartupState,
  rehydrateCoreState,
  type SteerDeliveryFact,
  type SteerDeliveryStep,
  type TodoItem,
  type UsageMeter,
  type WinnerSelection,
} from './state.js';
export {
  addTokens,
  EMPTY_LEDGER,
  NO_TOKENS,
  type TokenLedger,
  type TokenTally,
  totalTokens,
} from './token-usage.js';
