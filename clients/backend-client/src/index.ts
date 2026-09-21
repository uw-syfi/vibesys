export {
  type EventSubscription,
  ServerClient,
  type ServerClientOptions,
  type SubscribeOptions,
} from './client.js';
export {
  BackendClientError,
  type BackendErrorKind,
  isServerRejection,
  ServerError,
} from './errors.js';
export {
  PersistentEventStream,
  type PersistentEventStreamCallbacks,
  type PersistentEventStreamOptions,
  type StreamConnectionState,
  type StreamTransport,
} from './persistent-event-stream.js';
export type {
  AgentStatusData,
  ChatModelOption,
  ChatOptions,
  ChatProviderOptions,
  DesignFileChange,
  DesignPatch,
  DesignRound,
  Diagnostic,
  ExperimentCursor,
  ExperimentUpdate,
  HypothesisEntry,
  HypothesisRound,
  ProtocolRequest,
  ProtocolResponse,
  RequestInput,
  RunEvent,
  RunSnapshot,
  RunStatus,
  ServerMessage,
  TuiDefaults,
} from './protocol.js';
