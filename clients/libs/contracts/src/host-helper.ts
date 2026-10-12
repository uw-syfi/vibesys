/**
 * The host helper's contract as clients read it (`@vibesys/contracts/host-helper`).
 *
 * `server.host_api` owns every helper message; `host-helper.schema.json` is its language-neutral
 * export and the types below are generated from it. Every line a client reads from a helper goes
 * through `parseHelperMessage` (or `parseChannelEnd` for a relayed channel's stderr line), which
 * walks the generated descriptor strictly: the helper's models are `extra="forbid"` within a
 * version that `hello` negotiated, so an unknown field, member, or message type is a parse error
 * naming its path, never a value passed on.
 */
import {BackendClientError} from './errors.js';
import type {ChannelEnd, Message, Request} from './generated/host-helper.generated.js';
import hostHelperSchema from './generated/host-helper.schema.js';
import {type Schema, type SchemaWalk, validateSchema} from './schema-walker.js';

export type {
  ChannelEnd,
  ChannelEndOutcome,
  Checkout,
  DetachedLaunchFailure,
  FailureReason,
  HelloReply,
  HelperError,
  HelperErrorCode,
  HelperFeature,
  InstanceList,
  InstanceStatus,
  InstanceStopResult,
  InstancesChanged,
  LiveInstanceRecord,
  Message as HelperMessage,
  PathEntry,
  PathsCompleteReply,
  Request as HelperRequest,
  RunConnectReply,
  RunList,
  RunPhase,
  RunRole,
  RunStartReply,
  RunStatus,
  RunSummary,
  StopOutcome,
  TaskList,
  UnreadableRecord,
} from './generated/host-helper.generated.js';

const STRICT: SchemaWalk = {documents: [hostHelperSchema], strict: true};
const properties = hostHelperSchema['properties'] as Record<string, Schema>;
const MESSAGE = properties['message'] as Schema;
const CHANNEL_END = properties['channel_end'] as Schema;
const REQUEST = properties['request'] as Schema;

/** The helper protocol versions this client speaks (`hello`'s `client_versions`). */
export const HELPER_CLIENT_VERSIONS: readonly [number, number] = [1, 1];

/** Parse one line a helper wrote on a request channel; throws a `parse` error naming the path. */
export function parseHelperMessage(line: string): Message {
  const value = parseJson(line, 'helper message');
  validateSchema(value, MESSAGE, 'helper message', STRICT);
  return value as Message;
}

/**
 * Parse one request line as a helper does: for another implementation of the helper (a Fake, or a
 * helper in another language tested against this contract).
 */
export function parseHelperRequest(line: string): Request {
  const value = parseJson(line, 'helper request');
  validateSchema(value, REQUEST, 'helper request', STRICT);
  return value as Request;
}

/** Parse the `ChannelEnd` line a relayed run channel wrote on stderr. */
export function parseChannelEnd(line: string): ChannelEnd {
  const value = parseJson(line, 'channel end');
  validateSchema(value, CHANNEL_END, 'channel end', STRICT);
  return value as ChannelEnd;
}

function parseJson(line: string, path: string): unknown {
  try {
    return JSON.parse(line);
  } catch (error) {
    throw new BackendClientError('parse', `Invalid server ${path} JSON: ${String(error)}`, {
      cause: error,
    });
  }
}
