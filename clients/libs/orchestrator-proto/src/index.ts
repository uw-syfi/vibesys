/**
 * The orchestrator's contract as TypeScript: the messages, enums and service descriptor generated
 * from `proto/vibesys/orchestrator/v1` (never edited by hand; regenerate with
 * `buf generate --template buf.gen.orchestrator-ts.yaml`), and the one validated decoder.
 *
 * The orchestrator's protos are not part of the bindings of `@vibesys/proto`, so the existing
 * clients generate what they always did.
 */

export {sendMessageCommand} from './commands.js';
export {ORCHESTRATOR_MAX_MESSAGE_BYTES} from './framing.generated.js';
export type {ObjectiveBound} from './generated/vibesys/cli/v1/bound_pb.js';
export {
  AcceleratorModel,
  type AcceleratorNeed,
  type AcceleratorOffer,
  AcceleratorVendor,
} from './generated/vibesys/cli/v1/environment_pb.js';
export {JobState} from './generated/vibesys/cli/v1/job_pb.js';
export {type Plan, PlanSchema, PlanStatus} from './generated/vibesys/cli/v1/plan_pb.js';
export {Direction} from './generated/vibesys/cli/v1/result_pb.js';
export {
  type CardObjective,
  ModelMaker,
  type TaskCard,
  TaskCardSchema,
  TaskDomain,
  TaskObjectiveDirection,
} from './generated/vibesys/cli/v1/task_pb.js';
export {VerdictKind} from './generated/vibesys/cli/v1/verdict_pb.js';
export {
  type Workstream,
  WorkstreamStatus,
} from './generated/vibesys/cli/v1/workstream_pb.js';
export * from './generated/vibesys/orchestrator/v1/catalogue_pb.js';
export * from './generated/vibesys/orchestrator/v1/checkin_pb.js';
export * from './generated/vibesys/orchestrator/v1/cost_pb.js';
export * from './generated/vibesys/orchestrator/v1/eval_place_pb.js';
export * from './generated/vibesys/orchestrator/v1/events_pb.js';
export {
  HostControlService,
  HostPart,
  type IdentifyResponse,
  IdentifyResponseSchema,
  PartProblemReason,
  type PartReadiness,
  PartReadinessSchema,
  ReleaseRelation,
  StateRefusal,
  type Unserved,
  UnservedReason,
  UnservedSchema,
} from './generated/vibesys/orchestrator/v1/host_control_pb.js';
export * from './generated/vibesys/orchestrator/v1/orchestrator_pb.js';
export * from './generated/vibesys/orchestrator/v1/provision_pb.js';
export * from './generated/vibesys/orchestrator/v1/search_pb.js';
export * from './generated/vibesys/orchestrator/v1/service_pb.js';
export * from './generated/vibesys/orchestrator/v1/setup_pb.js';
export {oneofIs} from './oneof.js';
export {decodeValidated, decodeValidatedJson, encodeJson, encodeMessage} from './validated.js';
