/**
 * The run protocol's contract, shared by every client: the message types generated from the
 * Python models (`src/generated`, written by `pnpm generate:protocol`), the one runtime validator
 * that turns untrusted wire text into those types, and the error taxonomy that validator and the
 * transports report through. Runtime neutral: no DOM, no Node.
 */

export {
  type ArchitectureAnchor,
  type ArchitectureAnchorKind,
  type ArchitectureAttributionValue,
  type ArchitectureComponent,
  type ArchitectureComponentAttribution,
  type ArchitectureComponentChange,
  type ArchitectureComponentKind,
  type ArchitectureEdge,
  type ArchitectureEdgeKind,
  type ArchitectureGeneration,
  type ArchitectureGenerationApproach,
  type ArchitectureGraphDocument,
  type ArchitectureObservation,
  parseArchitectureGraph,
} from './architecture-graph.js';
export {
  BackendClientError,
  type BackendErrorKind,
  isServerRejection,
  ServerError,
} from './errors.js';
export type {
  AgentStatusData,
  ChatModelOption,
  ChatOptions,
  ChatProviderOptions,
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
  ProtocolRequest,
  ProtocolResponse,
  RequestInput,
  RunEvent,
  RunSnapshot,
  RunStatus,
  ServerMessage,
  TuiDefaults,
} from './protocol.js';
export {PROTOCOL_VERSION} from './protocol.js';
export {
  parseProtocolResponse,
  parseServerMessage,
  responseError,
  streamFailure,
  validateRunEvent,
} from './protocol-parse.js';
