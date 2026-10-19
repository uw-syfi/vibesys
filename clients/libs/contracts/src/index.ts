/**
 * The run protocol's contract, shared by every client: the message types generated from the
 * Python models (`src/generated`, written by `pnpm generate:protocol`), the one runtime validator
 * that turns untrusted wire text into those types, and the error taxonomy that validator and the
 * transports report through. Runtime neutral: no DOM, no Node.
 */

export {CHAT_AGENT_KIND, isChatAgentKind} from './agent-kinds.js';
export {CODE_IDS, declaredRoleName, roleName, wordsOfId} from './display-names.js';
export {
  BackendClientError,
  type BackendErrorKind,
  isServerRejection,
  ServerError,
} from './errors.js';
export type {
  ActiveExecutions,
  ActiveExecutionsChunkMessage,
  AgentStatusData,
  DesignFileChange,
  DesignPatch,
  DesignRound,
  Diagnostic,
  EventBatchMessage,
  ExperimentCursor,
  ExperimentUpdate,
  HypothesisEntry,
  HypothesisRound,
  IterationMetric,
  IterationPage,
  IterationResult,
  IterationSummary,
  IterationUnavailable,
  LogOwner,
  NarrationLevel,
  NarrationLine,
  ProtocolRequest,
  ProtocolResponse,
  RequestInput,
  RunEvent,
  RunOverview,
  RunSnapshot,
  RunStatus,
  ServerMessage,
  Span,
} from './protocol.js';
export {PROTOCOL_VERSION} from './protocol.js';
export {
  parseActiveExecutionsCheckpoint,
  parseProtocolResponse,
  parseServerMessage,
  responseError,
  streamFailure,
  validateRunEvent,
  validateRunSnapshot,
} from './protocol-parse.js';
export {isSynthesizedTask, SYNTHESIZED_TASK_NAME} from './synthesized-task.js';
