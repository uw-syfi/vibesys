import type {ProtocolDocument} from './generated/protocol.generated.js';

/**
 * The wire protocol version this client speaks. Source: `PROTOCOL_VERSION` in
 * `src/server/api/protocol.py`; a server that reports another one cannot be read by this client.
 */
export const PROTOCOL_VERSION = 1;

export type ProtocolRequest = ProtocolDocument['request'];
export type ProtocolResponse = ProtocolDocument['response'];
export type RunEvent = ProtocolDocument['event'];
/** Structured reply retained by an agent turn, using the producer's declared schema. */
export type AgentReply = NonNullable<
  Extract<NonNullable<RunEvent['data']>, {kind: 'agent_execution_finished'}>['reply']
>;
/** Structured status carried by streamed agent output and tool calls. */
export type AgentStatusData = NonNullable<
  Extract<NonNullable<RunEvent['data']>, {channel: unknown}>['status']
>;
export type RunSnapshot = ProtocolDocument['snapshot'];
/** The run's objective and what measures it. Source: `RunOverview` in `vibesys.events`. */
export type RunOverview = NonNullable<RunSnapshot['overview']>;
/** One span of the run trace: a flow task or an awaited operation. Source: `Span` in `vs_core.flows`. */
export type Span = NonNullable<RunSnapshot['spans']>[number];
/** Run lifecycle statuses the backend reports. Source: `RunStatus` in `src/server/run_lifecycle.py`. */
export type RunStatus = RunSnapshot['status'];
export type ServerMessage = ProtocolDocument['server_message'];
/** What a run's log stream is for. Source: `LogOwner` in `src/vibesys/log_owners.py`. */
export type LogOwner = ProtocolDocument['log_owner'];
/** One JSON line of a `run_narration` stream. Source: `NarrationLine` in `vs_logs`. */
export type NarrationLine = ProtocolDocument['narration_line'];
export type NarrationLevel = NarrationLine['level'];
/** The complete authoritative list staged from active-execution checkpoint chunks. */
export type ActiveExecutions = NonNullable<RunSnapshot['active_executions']>;
/** The `event_batch` arm of `ServerMessage`: a run of events plus cursor metadata. */
export type EventBatchMessage = Extract<ServerMessage, {type: 'event_batch'}>;
/** One transport-bounded piece of an authoritative active-execution checkpoint. */
export type ActiveExecutionsChunkMessage = Extract<
  ServerMessage,
  {type: 'active_executions_chunk'}
>;
export type Diagnostic = NonNullable<ProtocolResponse['diagnostic']>;
export type HypothesisEntry = NonNullable<ProtocolResponse['experiments']>[number];
export type ExperimentUpdate = NonNullable<ProtocolResponse['experiment_update']>;
export type ExperimentCursor = NonNullable<
  Extract<ProtocolRequest, {type?: 'query.experiments'}>['after']
>;
export type HypothesisRound = NonNullable<HypothesisEntry['rounds']>[number];
export type DesignRound = NonNullable<ProtocolResponse['design']>[number];
export type DesignFileChange = NonNullable<DesignRound['files']>[number];
export type DesignPatch = NonNullable<ProtocolResponse['design_patch']>;
export type IterationResult = NonNullable<ProtocolResponse['iterations']>;
export type IterationPage = Extract<IterationResult, {kind?: 'page'}>;
export type IterationUnavailable = Extract<IterationResult, {kind?: 'unavailable'}>;
export type IterationSummary = IterationPage['items'][number];
export type IterationMetric = NonNullable<IterationSummary['headline_metrics']>[number];

export type RequestInput = ProtocolRequest extends infer Request
  ? Request extends ProtocolRequest
    ? Omit<Request, 'protocol_version' | 'request_id' | 'client_id' | 'timestamp'>
    : never
  : never;
