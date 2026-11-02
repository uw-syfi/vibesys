/**
 * The orchestrator's contract as TypeScript: the messages, enums and service descriptor generated
 * from `proto/vibesys/orchestrator/v1` (never edited by hand; regenerate with
 * `buf generate --template buf.gen.orchestrator-ts.yaml`), and the one validated decoder.
 *
 * The orchestrator's protos are not part of the bindings of `@vibesys/proto`, so the existing
 * clients generate what they always did.
 */

export {
  type Command,
  CommandSchema,
  type Delta,
  type Event,
  EventSchema,
  type Receipt,
  Refusal,
} from './generated/vibesys/orchestrator/v1/events_pb.js';
export {
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  type Capabilities,
  Delivery,
  type Handoff,
  Harness,
  type Item,
  ItemStatus,
  type JobFinished,
  JobOutcome,
  type Message,
  Rejection,
  type Report,
  type Run,
  RunStatus,
  RunStopReason,
  type SubagentSettled,
  type Text,
  type ToolCall,
  type Turn,
  TurnStatus,
  TurnTrigger,
  type Usage,
} from './generated/vibesys/orchestrator/v1/orchestrator_pb.js';
export {
  type DispatchResponse,
  type ItemsPage,
  type ListItemsResponse,
  type ListPlansResponse,
  OrchestratorService,
  type Plan,
  type PlanComment,
  PlanStatus,
  type RunsSnapshot,
  type WatchAgentResponse,
  type WatchRunsResponse,
} from './generated/vibesys/orchestrator/v1/service_pb.js';
export {decodeValidated, encodeMessage} from './validated.js';
