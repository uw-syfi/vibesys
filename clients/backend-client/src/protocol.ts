import type {MessageInitShape} from '@bufbuild/protobuf';
import type {RequestSchema} from './gen/server/wire/v2/requests_pb.js';

/** Version of the wire contract in `proto/server/wire/v2`. Carried on every envelope. */
export const PROTOCOL_VERSION = 2;

export * from './gen/server/wire/v2/common_pb.js';
export * from './gen/server/wire/v2/events_pb.js';
export type {Request as ProtocolRequest} from './gen/server/wire/v2/requests_pb.js';
export * from './gen/server/wire/v2/requests_pb.js';
export type {Response as ProtocolResponse} from './gen/server/wire/v2/responses_pb.js';
export * from './gen/server/wire/v2/responses_pb.js';
export * from './gen/server/wire/v2/server_messages_pb.js';
export * from './gen/server/wire/v2/snapshot_pb.js';

/**
 * One request body as callers write it: a oneof case with the body message's
 * init shape, for example `{case: 'steer', value: {text: 'go'}}`.
 */
export type RequestBody = Exclude<
  NonNullable<MessageInitShape<typeof RequestSchema>['body']>,
  {case: undefined}
>;
