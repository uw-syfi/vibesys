/** Host and store schemas generated from proto/. Encode using @bufbuild/protobuf. */

export {API_SCHEMA_HASH, STORE_SCHEMA_HASH, RUN_LOG_MAX_RECORD_BYTES, RUN_LOG_MAGIC} from './generated/schema-hashes.js';
export {
  type HelloRequest,
  HelloRequestSchema,
  type HelloResponse,
  HelloResponseSchema,
  HostService,
  type TailRequest,
  TailRequestSchema,
  type TailResponse,
  TailResponseSchema,
} from './generated/vibesys/api/v1/host_pb.js';
export {
  type EpochStarted,
  EpochStartedSchema,
  type LegacyEvent,
  LegacyEventSchema,
  type Record,
  RecordSchema,
} from './generated/vibesys/store/v1/record_pb.js';
