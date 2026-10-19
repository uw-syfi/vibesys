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
import type {
  ChannelEnd,
  HelperFeature,
  Message,
  Request,
  RunStartCompensationFailure,
} from './generated/host-helper.generated.js';
import hostHelperSchema from './generated/host-helper.schema.js';
import {type Schema, type SchemaWalk, validateSchema} from './schema-walker.js';

export type {
  AccountLogin,
  AgentLogin,
  AgentLoginsListReply,
  AgentLoginsListRequest,
  AgentLoginsStartReply,
  AgentLoginsStartRequest,
  ApiKeyLogin,
  Backend,
  ChannelEnd,
  ChannelEndOutcome,
  Check,
  Checkout,
  CheckoutInspectReply,
  ClusterHardware,
  ClusterTestFailed,
  ClusterTestPassed,
  ComputeAddClusterReply,
  ComputeAddClusterRequest,
  ComputeListReply,
  ComputeListRequest,
  ComputeRemoveClusterReply,
  ComputeRemoveClusterRequest,
  ComputeSettings,
  ComputeTestClusterReply,
  ComputeTestClusterRequest,
  CreatedProject,
  DetachedLaunchFailure,
  ExampleDestination,
  ExampleHardware,
  ExampleList,
  ExampleModel,
  ExampleObjective,
  ExampleSummary,
  ExampleTask,
  ExampleWorkload,
  FailureReason,
  HarnessChoice,
  HarnessesListReply,
  HarnessOption,
  HelloReply,
  HelperError,
  HelperErrorCode,
  HelperFeature,
  IdeaVerdict,
  InstanceInspection,
  InstanceInspectionMetadata,
  InstanceList,
  InstanceStatus,
  InstanceStopResult,
  InstancesChanged,
  ListedCluster,
  LiveInstanceRecord,
  LogChunk,
  LoginStarted,
  LoginUnavailable,
  LogOffset,
  LogPipe,
  LogPipeSpan,
  LogStream,
  LogsListReply,
  LogsListRequest,
  LogsReadReply,
  LogsReadRequest,
  LogTail,
  MachineDescription,
  Message as HelperMessage,
  ModalStatus,
  ModalTarget,
  PathEntry,
  PathsCompleteReply,
  Request as HelperRequest,
  RunAttention,
  RunClosing,
  RunConnectReply,
  RunDigest,
  RunDigestUnreadable,
  RunEndingFact,
  RunList,
  RunOption,
  RunPhase,
  RunPreflightReply,
  RunPreflightRequest,
  RunRole,
  RunStartCompensationFailure,
  RunStartReply,
  RunStartRequest,
  RunStatus,
  RunSummary,
  RunsSummaryReply,
  RunsSummaryRequest,
  RunTerminal,
  SameMachine,
  SlurmClusterSettings,
  SlurmTarget,
  StopOutcome,
  TaskGoal,
  TaskList,
  TokenTally,
  TurnReply,
  UnreadableRecord,
  UsageRecord,
  UsageWindowReply,
  UsageWindowRequest,
} from './generated/host-helper.generated.js';

const STRICT: SchemaWalk = {documents: [hostHelperSchema], strict: true};
const properties = hostHelperSchema['properties'] as Record<string, Schema>;
const MESSAGE = properties['message'] as Schema;
const CHANNEL_END = properties['channel_end'] as Schema;
const COMPENSATION_FAILURE = properties['compensation_failure'] as Schema;
const REQUEST = properties['request'] as Schema;
const definitions = hostHelperSchema['$defs'] as Record<string, Schema>;
const featureByRequest = hostHelperSchema['x-vibesys-feature-by-request'] as Readonly<
  Partial<Record<Request['type'], HelperFeature>>
>;

/** The generated feature gate for each gated helper request. */
export const HELPER_FEATURE_BY_REQUEST = featureByRequest;

/** The feature `type` needs, or null when it is part of the base protocol. */
export function helperFeatureForRequest(type: string): HelperFeature | null {
  return Object.hasOwn(featureByRequest, type)
    ? (featureByRequest[type as Request['type']] ?? null)
    : null;
}

const featureByField = hostHelperSchema['x-vibesys-feature-by-field'] as Readonly<
  Partial<Record<Request['type'], Readonly<Record<string, HelperFeature>>>>
>;

/** The generated feature gate for each gated optional field, by request type then field name. */
export const HELPER_FEATURE_BY_FIELD = featureByField;

/**
 * Every feature `request` needs: its method's, and those of the gated optional fields it sets
 * (present and not null). A sender sends it only when `hello` returned them all.
 */
export function helperFeaturesForRequest(request: {
  readonly type: string;
}): readonly HelperFeature[] {
  const features = new Set<HelperFeature>();
  const method = helperFeatureForRequest(request.type);
  if (method !== null) features.add(method);
  const fields = Object.hasOwn(featureByField, request.type)
    ? featureByField[request.type as Request['type']]
    : undefined;
  for (const [name, feature] of Object.entries(fields ?? {})) {
    if ((request as Readonly<Record<string, unknown>>)[name] != null) features.add(feature);
  }
  return [...features];
}

const logsRead = definitions['LogsReadRequest'] as Schema;
const logsReadProperties = logsRead['properties'] as Record<string, Schema>;

/** The most bytes one `logs.read` returns (its `max_bytes` bound), from Python's contract. */
export const LOGS_READ_MAX_BYTES = (logsReadProperties['max_bytes'] as Schema)['maximum'] as number;

/** The longest one `logs.read` waits at the end of an open stream (its `wait_ms` bound). */
export const LOGS_WAIT_MAX_MS = (logsReadProperties['wait_ms'] as Schema)['maximum'] as number;

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

/** Parse the typed stderr report for an undisclosed start whose cleanup failed. */
export function parseRunStartCompensationFailure(line: string): RunStartCompensationFailure {
  const value = parseJson(line, 'run start compensation failure');
  validateSchema(value, COMPENSATION_FAILURE, 'run start compensation failure', STRICT);
  return value as RunStartCompensationFailure;
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
