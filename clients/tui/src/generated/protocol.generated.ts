/* Generated from the Python protocol models. Do not edit. */

export type Request =
  | PauseCommand
  | ResumeCommand
  | SnapshotQuery
  | ChatQuery
  | HistoryQuery
  | PerformanceQuery
  | EventsQuery
  | SubscribeRequest;
export type ProtocolVersion = 1;
export type RequestId = string;
export type Timestamp = string;
export type Type = "command.pause";
export type Mode = "after_current_agent_call";
export type ProtocolVersion1 = 1;
export type RequestId1 = string;
export type Timestamp1 = string;
export type Type1 = "command.resume";
export type ProtocolVersion2 = 1;
export type RequestId2 = string;
export type Timestamp2 = string;
export type Type2 = "query.snapshot";
export type ProtocolVersion3 = 1;
export type RequestId3 = string;
export type Timestamp3 = string;
export type Type3 = "query.chat";
export type Text = string;
export type ProtocolVersion4 = 1;
export type RequestId4 = string;
export type Timestamp4 = string;
export type Type4 = "query.history";
export type ProtocolVersion5 = 1;
export type RequestId5 = string;
export type Timestamp5 = string;
export type Type5 = "query.performance";
export type ProtocolVersion6 = 1;
export type RequestId6 = string;
export type Timestamp6 = string;
export type Type6 = "query.events";
export type AfterSequence = number;
export type TimeoutMs = number;
export type ProtocolVersion7 = 1;
export type RequestId7 = string;
export type Timestamp7 = string;
export type Type7 = "subscribe";
export type AfterSequence1 = number;
export type ProtocolVersion8 = 1;
export type RequestId8 = string;
export type Timestamp8 = string;
export type Ok = boolean;
export type Error = string | null;
export type Action = "pause" | "resume";
export type Status = "pending" | "consumed";
export type Question = string;
export type Answer = string;
export type Effect = "none";
export type ProtocolVersion9 = 1;
export type RunId = string;
export type Sequence = number;
export type Status1 = string;
export type AgentKind = string | null;
export type RoundLabel = string | null;
export type ProtocolVersion10 = 1;
export type Sequence1 = number;
export type RunId1 = string;
export type Timestamp9 = string;
export type EventType =
  | "server_started"
  | "server_ready"
  | "configuration_failed"
  | "run_started"
  | "run_interrupted"
  | "chat"
  | "status_query"
  | "control"
  | "invocation_started"
  | "invocation_finished"
  | "phase_started"
  | "phase_finished"
  | "agent_output_chunk"
  | "subprocess_output"
  | "judge_result"
  | "benchmark_result"
  | "round_finished"
  | "run_finished"
  | "run_failed"
  | "output"
  | "tool_call"
  | "tool_result"
  | "todo_update"
  | "usage_update";
export type Text1 = string;
export type EventStatus = "active" | "answered" | "pending" | "consumed" | "completed" | "failed";
export type RoundLabel1 = string | null;
export type AgentKind1 = string | null;
export type InvocationId = string | null;
export type Data =
  | (
      | ChatData
      | InvocationStartedData
      | InvocationFinishedData
      | OutputData
      | ServerReadyData
      | RunStartedData
      | RunInterruptedData
      | ConfigurationFailedData
      | PhaseData
      | AgentOutputChunkData
      | SubprocessOutputData
      | JudgeResultData
      | BenchmarkResultData
      | RoundFinishedData
      | ToolCallData
      | ToolResultData
      | TodoUpdateData
      | UsageUpdateData
    )
  | null;
export type Kind = "chat";
export type Answer1 = string;
export type Kind1 = "invocation_started";
export type SystemPrompt = string;
export type UserPrompt = string;
export type Kind2 = "invocation_finished";
export type Error1 = string | null;
export type Kind3 = "output";
export type Stream = "stdout" | "stderr";
export type Source = string;
export type Content = string;
export type Kind4 = "server_ready";
export type SocketProtocol = "jsonl";
export type Kind5 = "run_started";
export type OuterLoop = string;
export type Input = string;
export type MaxRounds = number;
export type Kind6 = "run_interrupted";
export type Reason = string;
export type Signal = string | null;
export type Kind7 = "configuration_failed";
export type Code = string;
export type Stage = string;
export type Message = string;
export type Usage = string | null;
export type ExitCode = number;
export type Kind8 = "phase";
export type Phase = string;
export type Attempt = number | null;
export type Kind9 = "agent_output_chunk";
export type Channel = "assistant" | "analysis" | "tool" | "diagnostic" | "prompt";
export type Content1 = string;
export type Progress = string | null;
export type AgentLabel = string | null;
export type ElapsedSeconds = number;
export type InputTokens = number;
export type ContextWindow = number | null;
export type Kind10 = "subprocess_output";
export type ProcessId = string;
export type ProcessKind = string;
export type Stream1 = "stdout" | "stderr";
export type Content2 = string;
export type Kind11 = "judge_result";
export type Verdict = "pass" | "fail";
export type Feedback = string;
export type Attempt1 = number;
export type Kind12 = "benchmark_result";
export type Metric = string;
export type Value = number;
export type Unit = string;
export type Kind13 = "round_finished";
export type Attempts = number;
export type JudgeVerdict = "pass" | "fail";
export type PerfMetric = number | null;
export type PerfUnit = string | null;
export type Kind14 = "tool_call";
export type Tool = string;
export type CallId = string | null;
export type Kind15 = "tool_result";
export type Tool1 = string;
export type CallId1 = string | null;
export type Content3 = string;
export type IsError = boolean;
export type Kind16 = "todo_update";
export type Content4 = string;
export type Status2 = string;
export type Todos = TodoItemData[];
export type Kind17 = "usage_update";
export type InputTokens1 = number;
export type ContextWindow1 = number | null;
export type Model = string | null;
export type Events = RunEvent[];
export type Round = number;
export type PerfMetric1 = number;
export type PerfUnit1 = string;
export type Passed = boolean;
export type ProfileSkipped = boolean;
export type Performance = PerformanceRound[];
export type ServerMessage = SubscribedMessage | EventMessage | EventBatchMessage | ProtocolErrorMessage;
export type Type8 = "subscribed";
export type RequestId9 = string;
export type RunId2 = string;
export type LatestSequence = number;
export type Type9 = "event";
export type Type10 = "event_batch";
export type Events1 = RunEvent[];
export type Type11 = "protocol_error";
export type RequestId10 = string | null;
export type Code1 = string;
export type Message1 = string;

export interface ProtocolDocument {
  request: Request;
  response: Response;
  event: RunEvent;
  snapshot: RunSnapshot;
  server_message: ServerMessage;
  [k: string]: unknown;
}
export interface PauseCommand {
  protocol_version?: ProtocolVersion;
  request_id?: RequestId;
  timestamp?: Timestamp;
  type?: Type;
  mode?: Mode;
}
export interface ResumeCommand {
  protocol_version?: ProtocolVersion1;
  request_id?: RequestId1;
  timestamp?: Timestamp1;
  type?: Type1;
}
export interface SnapshotQuery {
  protocol_version?: ProtocolVersion2;
  request_id?: RequestId2;
  timestamp?: Timestamp2;
  type?: Type2;
}
export interface ChatQuery {
  protocol_version?: ProtocolVersion3;
  request_id?: RequestId3;
  timestamp?: Timestamp3;
  type?: Type3;
  text: Text;
}
export interface HistoryQuery {
  protocol_version?: ProtocolVersion4;
  request_id?: RequestId4;
  timestamp?: Timestamp4;
  type?: Type4;
}
export interface PerformanceQuery {
  protocol_version?: ProtocolVersion5;
  request_id?: RequestId5;
  timestamp?: Timestamp5;
  type?: Type5;
}
export interface EventsQuery {
  protocol_version?: ProtocolVersion6;
  request_id?: RequestId6;
  timestamp?: Timestamp6;
  type?: Type6;
  after_sequence?: AfterSequence;
  timeout_ms?: TimeoutMs;
}
export interface SubscribeRequest {
  protocol_version?: ProtocolVersion7;
  request_id?: RequestId7;
  timestamp?: Timestamp7;
  type?: Type7;
  after_sequence?: AfterSequence1;
}
export interface Response {
  protocol_version?: ProtocolVersion8;
  request_id: RequestId8;
  timestamp?: Timestamp8;
  ok?: Ok;
  error?: Error;
  ack?: CommandAck | null;
  chat?: ChatResult | null;
  snapshot?: RunSnapshot | null;
  events?: Events;
  performance?: Performance;
}
export interface CommandAck {
  action: Action;
  status: Status;
}
export interface ChatResult {
  question: Question;
  answer: Answer;
  effect?: Effect;
}
export interface RunSnapshot {
  protocol_version?: ProtocolVersion9;
  run_id: RunId;
  sequence: Sequence;
  status: Status1;
  agent_kind?: AgentKind;
  round_label?: RoundLabel;
}
/**
 * One reproducible human, control, or invocation event.
 */
export interface RunEvent {
  protocol_version?: ProtocolVersion10;
  sequence?: Sequence1;
  run_id?: RunId1;
  timestamp: Timestamp9;
  type: EventType;
  text?: Text1;
  status?: EventStatus | null;
  round_label?: RoundLabel1;
  agent_kind?: AgentKind1;
  invocation_id?: InvocationId;
  data?: Data;
}
export interface ChatData {
  kind?: Kind;
  answer: Answer1;
  [k: string]: unknown;
}
export interface InvocationStartedData {
  kind?: Kind1;
  system_prompt: SystemPrompt;
  user_prompt: UserPrompt;
  [k: string]: unknown;
}
export interface InvocationFinishedData {
  kind?: Kind2;
  result?: Result;
  error?: Error1;
  [k: string]: unknown;
}
export interface Result {
  [k: string]: unknown;
}
export interface OutputData {
  kind?: Kind3;
  stream: Stream;
  source?: Source;
  content: Content;
  [k: string]: unknown;
}
export interface ServerReadyData {
  kind?: Kind4;
  socket_protocol?: SocketProtocol;
  [k: string]: unknown;
}
export interface RunStartedData {
  kind?: Kind5;
  outer_loop: OuterLoop;
  input: Input;
  max_rounds: MaxRounds;
  [k: string]: unknown;
}
export interface RunInterruptedData {
  kind?: Kind6;
  reason: Reason;
  signal?: Signal;
  [k: string]: unknown;
}
export interface ConfigurationFailedData {
  kind?: Kind7;
  code: Code;
  stage: Stage;
  message: Message;
  usage?: Usage;
  exit_code: ExitCode;
  [k: string]: unknown;
}
export interface PhaseData {
  kind?: Kind8;
  phase: Phase;
  attempt?: Attempt;
  [k: string]: unknown;
}
export interface AgentOutputChunkData {
  kind?: Kind9;
  channel: Channel;
  content: Content1;
  status?: AgentStatusData | null;
  [k: string]: unknown;
}
/**
 * Structured progress readings for one agent invocation.
 *
 * Carried on presentation events so renderers can format their own status
 * prefix (e.g. ``[Round 3/24 | Implementer | 12.3s | 20k/1.0M]``) without
 * the backend baking any layout or styling into the payload.
 */
export interface AgentStatusData {
  progress?: Progress;
  agent_label?: AgentLabel;
  elapsed_seconds?: ElapsedSeconds;
  input_tokens?: InputTokens;
  context_window?: ContextWindow;
  [k: string]: unknown;
}
export interface SubprocessOutputData {
  kind?: Kind10;
  process_id: ProcessId;
  process_kind: ProcessKind;
  stream: Stream1;
  content: Content2;
  [k: string]: unknown;
}
export interface JudgeResultData {
  kind?: Kind11;
  verdict: Verdict;
  feedback: Feedback;
  attempt: Attempt1;
  [k: string]: unknown;
}
export interface BenchmarkResultData {
  kind?: Kind12;
  metric: Metric;
  value: Value;
  unit: Unit;
  [k: string]: unknown;
}
export interface RoundFinishedData {
  kind?: Kind13;
  attempts: Attempts;
  judge_verdict: JudgeVerdict;
  perf_metric?: PerfMetric;
  perf_unit?: PerfUnit;
  [k: string]: unknown;
}
export interface ToolCallData {
  kind?: Kind14;
  tool: Tool;
  call_id?: CallId;
  args?: Args;
  status?: AgentStatusData | null;
  [k: string]: unknown;
}
export interface Args {
  [k: string]: unknown;
}
export interface ToolResultData {
  kind?: Kind15;
  tool: Tool1;
  call_id?: CallId1;
  content: Content3;
  is_error?: IsError;
  [k: string]: unknown;
}
export interface TodoUpdateData {
  kind?: Kind16;
  todos?: Todos;
  [k: string]: unknown;
}
export interface TodoItemData {
  content: Content4;
  status: Status2;
  [k: string]: unknown;
}
export interface UsageUpdateData {
  kind?: Kind17;
  input_tokens: InputTokens1;
  context_window?: ContextWindow1;
  model?: Model;
  [k: string]: unknown;
}
export interface PerformanceRound {
  round: Round;
  perf_metric: PerfMetric1;
  perf_unit: PerfUnit1;
  passed: Passed;
  profile_skipped?: ProfileSkipped;
}
export interface SubscribedMessage {
  type?: Type8;
  request_id: RequestId9;
  run_id: RunId2;
  latest_sequence: LatestSequence;
}
export interface EventMessage {
  type?: Type9;
  event: RunEvent;
}
export interface EventBatchMessage {
  type?: Type10;
  events: Events1;
}
export interface ProtocolErrorMessage {
  type?: Type11;
  request_id?: RequestId10;
  code: Code1;
  message: Message1;
}
