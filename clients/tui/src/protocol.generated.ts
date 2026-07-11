/* Generated from the Python protocol models. Do not edit. */

export type Request =
  | SteerCommand
  | PauseCommand
  | ResumeCommand
  | StatusQuery
  | SnapshotQuery
  | ChatQuery
  | HistoryQuery
  | RoundQuery
  | InvocationQuery
  | ArtifactQuery
  | EventsQuery;
export type ProtocolVersion = 1;
export type RequestId = string;
export type Timestamp = string;
export type Type = "command.steer";
export type Text = string;
export type Target = "next_safe_point";
export type ProtocolVersion1 = 1;
export type RequestId1 = string;
export type Timestamp1 = string;
export type Type1 = "command.pause";
export type Mode = "after_current_agent_call";
export type ProtocolVersion2 = 1;
export type RequestId2 = string;
export type Timestamp2 = string;
export type Type2 = "command.resume";
export type ProtocolVersion3 = 1;
export type RequestId3 = string;
export type Timestamp3 = string;
export type Type3 = "query.status";
export type ProtocolVersion4 = 1;
export type RequestId4 = string;
export type Timestamp4 = string;
export type Type4 = "query.snapshot";
export type ProtocolVersion5 = 1;
export type RequestId5 = string;
export type Timestamp5 = string;
export type Type5 = "query.chat";
export type Text1 = string;
export type ProtocolVersion6 = 1;
export type RequestId6 = string;
export type Timestamp6 = string;
export type Type6 = "query.history";
export type ProtocolVersion7 = 1;
export type RequestId7 = string;
export type Timestamp7 = string;
export type Type7 = "query.round";
export type RoundNumber = number;
export type ProtocolVersion8 = 1;
export type RequestId8 = string;
export type Timestamp8 = string;
export type Type8 = "query.invocation";
export type InvocationId = string;
export type ProtocolVersion9 = 1;
export type RequestId9 = string;
export type Timestamp9 = string;
export type Type9 = "query.artifact";
export type Path = string;
export type ProtocolVersion10 = 1;
export type RequestId10 = string;
export type Timestamp10 = string;
export type Type10 = "query.events";
export type AfterSequence = number;
export type TimeoutMs = number;
export type ProtocolVersion11 = 1;
export type RequestId11 = string;
export type Timestamp11 = string;
export type Ok = boolean;
export type Error = string | null;
export type Action = "steer" | "pause" | "resume";
export type Status = "pending" | "consumed";
export type ResourceId = string | null;
export type Question = string;
export type Answer = string;
export type Effect = "none";
export type RoundNumber1 = number;
export type Source = string;
export type Content = string;
export type Blocks = TextBlock[];
export type Path1 = string;
export type Content1 = string;
export type ProtocolVersion12 = 1;
export type RunId = string;
export type Sequence = number;
export type Status1 = string;
export type AgentKind = string | null;
export type RoundLabel = string | null;
export type PendingSteering = number;
export type LastConsumedSteering = string | null;
export type ProtocolVersion13 = 1;
export type Sequence1 = number;
export type RunId1 = string;
export type Timestamp12 = string;
export type EventType =
  | "server_started"
  | "tui_started"
  | "chat"
  | "status_query"
  | "steering"
  | "control"
  | "invocation_started"
  | "invocation_finished"
  | "run_finished"
  | "run_failed"
  | "output";
export type Text2 = string;
export type EventStatus = "active" | "answered" | "pending" | "consumed" | "completed" | "failed";
export type RoundLabel1 = string | null;
export type AgentKind1 = string | null;
export type InvocationId1 = string | null;
export type SteeringIds = string[];
export type Data =
  (ChatData | SteeringData | InvocationStartedData | InvocationFinishedData | ArtifactData | OutputData) | null;
export type Kind = "chat";
export type Answer1 = string;
export type Kind1 = "steering";
export type SteeringId = string;
export type Kind2 = "invocation_started";
export type SystemPrompt = string;
export type UserPrompt = string;
export type Kind3 = "invocation_finished";
export type Error1 = string | null;
export type Kind4 = "artifact";
export type Path2 = string;
export type Kind5 = "output";
export type Stream = "stdout" | "stderr";
export type Source1 = string;
export type Content2 = string;
export type Events = RunEvent[];

export interface ProtocolDocument {
  request: Request;
  response: Response;
  event: RunEvent;
  snapshot: RunSnapshot;
  [k: string]: unknown;
}
export interface SteerCommand {
  protocol_version?: ProtocolVersion;
  request_id?: RequestId;
  timestamp?: Timestamp;
  type?: Type;
  text: Text;
  target?: Target;
}
export interface PauseCommand {
  protocol_version?: ProtocolVersion1;
  request_id?: RequestId1;
  timestamp?: Timestamp1;
  type?: Type1;
  mode?: Mode;
}
export interface ResumeCommand {
  protocol_version?: ProtocolVersion2;
  request_id?: RequestId2;
  timestamp?: Timestamp2;
  type?: Type2;
}
export interface StatusQuery {
  protocol_version?: ProtocolVersion3;
  request_id?: RequestId3;
  timestamp?: Timestamp3;
  type?: Type3;
}
export interface SnapshotQuery {
  protocol_version?: ProtocolVersion4;
  request_id?: RequestId4;
  timestamp?: Timestamp4;
  type?: Type4;
}
export interface ChatQuery {
  protocol_version?: ProtocolVersion5;
  request_id?: RequestId5;
  timestamp?: Timestamp5;
  type?: Type5;
  text: Text1;
}
export interface HistoryQuery {
  protocol_version?: ProtocolVersion6;
  request_id?: RequestId6;
  timestamp?: Timestamp6;
  type?: Type6;
}
export interface RoundQuery {
  protocol_version?: ProtocolVersion7;
  request_id?: RequestId7;
  timestamp?: Timestamp7;
  type?: Type7;
  round_number: RoundNumber;
}
export interface InvocationQuery {
  protocol_version?: ProtocolVersion8;
  request_id?: RequestId8;
  timestamp?: Timestamp8;
  type?: Type8;
  invocation_id: InvocationId;
}
export interface ArtifactQuery {
  protocol_version?: ProtocolVersion9;
  request_id?: RequestId9;
  timestamp?: Timestamp9;
  type?: Type9;
  path: Path;
}
export interface EventsQuery {
  protocol_version?: ProtocolVersion10;
  request_id?: RequestId10;
  timestamp?: Timestamp10;
  type?: Type10;
  after_sequence?: AfterSequence;
  timeout_ms?: TimeoutMs;
}
export interface Response {
  protocol_version?: ProtocolVersion11;
  request_id: RequestId11;
  timestamp?: Timestamp11;
  ok?: Ok;
  error?: Error;
  ack?: CommandAck | null;
  chat?: ChatResult | null;
  round?: RoundResult | null;
  artifact?: ArtifactResult | null;
  snapshot?: RunSnapshot | null;
  events?: Events;
}
export interface CommandAck {
  action: Action;
  status: Status;
  resource_id?: ResourceId;
}
export interface ChatResult {
  question: Question;
  answer: Answer;
  effect?: Effect;
}
export interface RoundResult {
  round_number: RoundNumber1;
  blocks?: Blocks;
}
export interface TextBlock {
  source: Source;
  content: Content;
}
export interface ArtifactResult {
  path: Path1;
  content: Content1;
}
export interface RunSnapshot {
  protocol_version?: ProtocolVersion12;
  run_id: RunId;
  sequence: Sequence;
  status: Status1;
  agent_kind?: AgentKind;
  round_label?: RoundLabel;
  pending_steering?: PendingSteering;
  last_consumed_steering?: LastConsumedSteering;
}
/**
 * One reproducible human, control, or invocation event.
 */
export interface RunEvent {
  protocol_version?: ProtocolVersion13;
  sequence?: Sequence1;
  run_id?: RunId1;
  timestamp: Timestamp12;
  type: EventType;
  text?: Text2;
  status?: EventStatus | null;
  round_label?: RoundLabel1;
  agent_kind?: AgentKind1;
  invocation_id?: InvocationId1;
  steering_ids?: SteeringIds;
  data?: Data;
}
export interface ChatData {
  kind?: Kind;
  answer: Answer1;
  [k: string]: unknown;
}
export interface SteeringData {
  kind?: Kind1;
  steering_id: SteeringId;
  [k: string]: unknown;
}
export interface InvocationStartedData {
  kind?: Kind2;
  system_prompt: SystemPrompt;
  user_prompt: UserPrompt;
  [k: string]: unknown;
}
export interface InvocationFinishedData {
  kind?: Kind3;
  result?: Result;
  error?: Error1;
  [k: string]: unknown;
}
export interface Result {
  [k: string]: unknown;
}
export interface ArtifactData {
  kind?: Kind4;
  path: Path2;
  [k: string]: unknown;
}
export interface OutputData {
  kind?: Kind5;
  stream: Stream;
  source?: Source1;
  content: Content2;
  [k: string]: unknown;
}
