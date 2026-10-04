/** Published projection contract. Replay/merge machinery stays package-private. */
export {
  type ActiveAgentExecution,
  type ActiveExecutionCheckpoint,
  type AgentExecutionMode,
  type BenchmarkRecord,
  type ChatThread,
  type CoreDiagnostic,
  type CoreRunStatus,
  type CoreState,
  DEFAULT_CHAT_THREAD_ID,
  type EndedRunStatus,
  type ExecutionTodos,
  hasRunEnded,
  initialCoreState,
  latestDiagnosticChange,
  type RunLifetimeBoundary,
  reconcileActiveExecutions,
  recordsBenchmark,
  reduceEvent,
  reduceEventBatch,
  reduceEventPrefix,
  reduceEventRebootstrap,
  reduceResponseEvents,
  reduceSnapshot,
  type TodoItem,
  type ToolResultPayload,
  type TranscriptEntry,
  type UsageMeter,
} from './core-state.js';
export {type ExecutionStatus, executionStatusFor} from './execution-status.js';
export {
  activeRunFocus,
  agentKindText,
  describePhase,
  phaseText,
  planningStageForPhase,
} from './phase-semantics.js';
export {type RoundKey, roundKeyFor, sameRoundKey} from './round-key.js';
export {
  experimentForRound,
  hypothesisRoundNumbers,
  joinRoundsWithExperiments,
  type RoundOutcome,
  roundOutcome,
  roundsWithPlan,
} from './round-projection.js';
export {hasActiveAgentTiming} from './round-timing.js';
export {
  type AgentPhase,
  phasesForRound,
  type RoundState,
  roundAgentElapsedMs,
} from './run-map.js';
