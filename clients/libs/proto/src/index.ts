/** Host and store schemas generated from proto/. Encode using @bufbuild/protobuf. */

export {descriptorRegistry} from './descriptor-registry.js';
export {
  HOST_API_MAX_MESSAGE_BYTES,
  RUN_LOG_MAGIC,
  RUN_LOG_MAX_RECORD_BYTES,
} from './generated/framing.js';
export * from './generated/host-operations.js';
export * from './generated/vibesys/api/v1/host_call_pb.js';
export {
  type MutateRequest,
  MutateRequestSchema,
  type MutateResponse,
  MutateResponseSchema,
} from './generated/vibesys/api/v1/host_mutate_generated_pb.js';
export {
  BlobResultSchema,
  DescriptorSetSchema,
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  type HelloRequest,
  HelloRequestSchema,
  type HelloResponse,
  HelloResponseSchema,
  HostService,
  ListChatSessionsRequestSchema,
  type ListChatSessionsResponse,
  ListChatSessionsResponseSchema,
  SkeletonRequestSchema,
  type SkeletonResponse,
  SkeletonResponseSchema,
  type TailRequest,
  TailRequestSchema,
  type TailResponse,
  TailResponseSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
} from './generated/vibesys/api/v1/host_pb.js';
export {
  type ReadRequest,
  ReadRequestSchema,
  type ReadResponse,
  ReadResponseSchema,
} from './generated/vibesys/api/v1/host_read_generated_pb.js';
export {
  type WatchRequest,
  WatchRequestSchema,
  type WatchResponse,
  WatchResponseSchema,
} from './generated/vibesys/api/v1/host_watch_generated_pb.js';
export * from './generated/vibesys/api/v1/subscribe_pb.js';
export {CheckoutStatus} from './generated/vibesys/core/v1/vocabulary_pb.js';
export * from './generated/vibesys/desktop/v1/gateway_pb.js';
export * from './generated/vibesys/desktop/v1/settings_pb.js';
export * from './generated/vibesys/desktop/v1/subscription_pb.js';
export * from './generated/vibesys/host/v1/failure_pb.js';
export * from './generated/vibesys/host/v1/operations_pb.js';
export * from './generated/vibesys/host/v1/usage_state_pb.js';
export * from './generated/vibesys/search/v1/attrs_pb.js';
export * from './generated/vibesys/store/v1/agent_attrs_pb.js';
export * from './generated/vibesys/store/v1/chat_pb.js';
export * from './generated/vibesys/store/v1/common_pb.js';
export * from './generated/vibesys/store/v1/content_pb.js';
export * from './generated/vibesys/store/v1/environment_attrs_pb.js';
export * from './generated/vibesys/store/v1/epoch_pb.js';
export * from './generated/vibesys/store/v1/intent_pb.js';
export * from './generated/vibesys/store/v1/lifecycle_pb.js';
export * from './generated/vibesys/store/v1/notice_pb.js';
export {
  type EpochStarted,
  EpochStartedSchema,
  type LegacyEvent,
  LegacyEventSchema,
  type Record,
  RecordSchema,
} from './generated/vibesys/store/v1/record_pb.js';
export * from './generated/vibesys/store/v1/skeleton_pb.js';
export * from './generated/vibesys/store/v1/span_pb.js';
export * from './generated/vibesys/store/v1/transcript_pb.js';
export {ClientReplaySchema} from './generated/vibesys/testing/v1/client_replay_pb.js';
export {HOST_UNSUPPORTED_MESSAGE, OperationFailed} from './operation-failure.js';
export type {Accepted, OperationSignal, OperationTransport} from './operations.js';
export {outcomePresentation, projectLifecycle} from './outcome.js';
export {projectMessage} from './project.js';
export {API_SCHEMA_HASH, HOST_SCHEMA_HASH, STORE_SCHEMA_HASH, schemaHash} from './schema-hashes.js';
export {validatedExactFromBinary, validatedFromBinary, validateHostPayload} from './validation.js';
