/* Generated from the Python protocol models. Do not edit. */

export type Request = PauseCommand | ResumeCommand | SnapshotQuery | ChatQuery | HistoryQuery | EventsQuery;
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
export type Type5 = "query.events";
export type AfterSequence = number;
export type TimeoutMs = number;
export type ProtocolVersion6 = 1;
export type RequestId6 = string;
export type Timestamp6 = string;
export type Ok = boolean;
export type Error = string | null;
export type Action = "pause" | "resume";
export type Status = "pending" | "consumed";
export type Question = string;
export type Answer = string;
export type Effect = "none";
export type ProtocolVersion7 = 1;
export type RunId = string;
export type Sequence = number;
export type Status1 = string;
export type AgentKind = string | null;
export type RoundLabel = string | null;
export type ProtocolVersion8 = 1;
export type Sequence1 = number;
export type RunId1 = string;
export type Timestamp7 = string;
export type EventType =
  | "server_started"
  | "chat"
  | "status_query"
  | "control"
  | "invocation_started"
  | "invocation_finished"
  | "run_finished"
  | "run_failed"
  | "output";
export type Text1 = string;
export type EventStatus = "active" | "answered" | "pending" | "consumed" | "completed" | "failed";
export type RoundLabel1 = string | null;
export type AgentKind1 = string | null;
export type InvocationId = string | null;
export type Data = (ChatData | InvocationStartedData | InvocationFinishedData | OutputData) | null;
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
export type Events = RunEvent[];

export interface ProtocolDocument {
  request: Request;
  response: Response;
  event: RunEvent;
  snapshot: RunSnapshot;
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
export interface EventsQuery {
  protocol_version?: ProtocolVersion5;
  request_id?: RequestId5;
  timestamp?: Timestamp5;
  type?: Type5;
  after_sequence?: AfterSequence;
  timeout_ms?: TimeoutMs;
}
export interface Response {
  protocol_version?: ProtocolVersion6;
  request_id: RequestId6;
  timestamp?: Timestamp6;
  ok?: Ok;
  error?: Error;
  ack?: CommandAck | null;
  chat?: ChatResult | null;
  snapshot?: RunSnapshot | null;
  events?: Events;
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
  protocol_version?: ProtocolVersion7;
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
  protocol_version?: ProtocolVersion8;
  sequence?: Sequence1;
  run_id?: RunId1;
  timestamp: Timestamp7;
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
