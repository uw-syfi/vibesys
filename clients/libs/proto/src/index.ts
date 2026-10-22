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
  BlobResultSchema,
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  type HelloRequest,
  HelloRequestSchema,
  type HelloResponse,
  HelloResponseSchema,
  HostService,
  RunLiveness,
  SkeletonRequestSchema,
  type SkeletonResponse,
  SkeletonResponseSchema,
  type TailRequest,
  TailRequestSchema,
  type TailResponse,
  TailResponseSchema,
  TailStreamRequestSchema,
  TailStreamResponseSchema,
  WatchRunsRequestSchema,
  type WatchRunsResponse,
  WatchRunsResponseSchema,
} from './generated/vibesys/api/v1/host_pb.js';
export * from './generated/vibesys/host/v1/failure_pb.js';
export * from './generated/vibesys/host/v1/operations_pb.js';
export * from './generated/vibesys/search/v1/attrs_pb.js';
export * from './generated/vibesys/store/v1/agent_attrs_pb.js';
export * from './generated/vibesys/store/v1/chat_pb.js';
export * from './generated/vibesys/store/v1/common_pb.js';
export * from './generated/vibesys/store/v1/content_pb.js';
export * from './generated/vibesys/store/v1/environment_attrs_pb.js';
export * from './generated/vibesys/store/v1/epoch_pb.js';
export * from './generated/vibesys/store/v1/intent_pb.js';
export * from './generated/vibesys/store/v1/lifecycle_pb.js';
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
export type {Accepted, OperationSignal, OperationTransport} from './operations.js';
export {API_SCHEMA_HASH, HOST_SCHEMA_HASH, STORE_SCHEMA_HASH, schemaHash} from './schema-hashes.js';

export {validatedFromBinary, validateHostPayload} from './validation.js';
