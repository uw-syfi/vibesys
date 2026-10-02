import {BackendClientError, ServerError} from './errors.js';
import type {Diagnostic, ProtocolResponse, ServerMessage} from './protocol.js';

const RESPONSE = 'response';
const STREAM = 'event-stream message';

/**
 * Parse and validate a response independently of the transport framing.
 *
 * The envelope and the two payloads that leave this package as protocol values
 * are checked: every event in `events`, which the folds apply exactly as a
 * streamed batch's events are applied, and `diagnostic`, which `responseError`
 * hands to callers inside a `ServerError`. The remaining payload fields a
 * response can carry (`snapshot`, `experiments`, `design`, `chat`,
 * `chat_options`, `tui_defaults`, `performance`, `ack`, ...) are fifteen
 * further nested models that no event-stream frame reaches, so the assertion
 * below is earned for the envelope, the events, and the diagnostic, and is
 * still an assumption for the rest. Closing that is the response half of this
 * boundary and wants its own change rather than a partial descent here.
 */
export function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseJson(line, RESPONSE);
  const record = requireRecord(value, RESPONSE);
  if (record['protocol_version'] !== 1) {
    throw new BackendClientError('parse', 'Unsupported server protocol version');
  }
  requireString(record, 'request_id', RESPONSE);
  requireBoolean(record, 'ok', RESPONSE);
  optionalString(record, 'client_id', RESPONSE);
  optionalString(record, 'timestamp', RESPONSE);
  // Nullable, not merely optional: the server serializes its whole model, so a
  // successful response carries `error: null` rather than omitting the key.
  nullableString(record, 'error', RESPONSE);
  validateDiagnostic(record, RESPONSE);
  if (record['events'] !== undefined) validateEventList(record, 'events', RESPONSE);
  return value as ProtocolResponse;
}

/**
 * Parse and validate a streamed server message independently of framing.
 *
 * Every field each `ServerMessage` member declares is checked before the
 * assertion below, items in `events` and `active_executions` included, so the
 * assertion states what the switch and the validators have established rather
 * than what the server promised.
 */
export function parseServerMessage(line: string): ServerMessage {
  const value = parseJson(line, STREAM);
  const record = requireRecord(value, STREAM);
  switch (record['type']) {
    case 'subscribed':
      validateSubscribed(record);
      break;
    case 'event':
      validateRunEvent(record['event'], `${STREAM} event`);
      break;
    case 'event_batch':
      validateEventBatch(record);
      break;
    case 'protocol_error':
      validateProtocolError(record);
      break;
    default:
      throw unknownStreamLineError(record);
  }
  return value as ServerMessage;
}

/** Convert an invalid frame or callback exception into the public error taxonomy. */
export function streamFailure(error: unknown): BackendClientError {
  if (error instanceof BackendClientError) return error;
  const cause = toError(error);
  return new BackendClientError('parse', cause.message, {cause});
}

export function responseError(response: ProtocolResponse): ServerError {
  return new ServerError(response.error ?? 'Unknown server error', response.diagnostic ?? null);
}

/**
 * A server that predates an optional subscribe field rejects it with a Response
 * frame on the stream connection. Treat that as a capability refusal, not a
 * malformed stream, so callers can probe and fall back.
 */
function unknownStreamLineError(value: Record<string, unknown>): BackendClientError {
  const rejected =
    value['type'] === undefined && value['ok'] === false && typeof value['request_id'] === 'string';
  if (!rejected) {
    return new BackendClientError(
      'parse',
      `Unknown server event-stream message: ${String(value['type'])}`,
    );
  }
  return new ServerError(
    typeof value['error'] === 'string' ? value['error'] : 'Server rejected the subscription',
    validateDiagnostic(value, STREAM),
  );
}

function validateSubscribed(record: Record<string, unknown>): void {
  requireString(record, 'request_id', STREAM);
  optionalString(record, 'client_id', STREAM);
  requireString(record, 'run_id', STREAM);
  requireNumber(record, 'latest_sequence', STREAM);
}

/**
 * Malformed-item policy: typed rejection of the whole frame, not a counted drop.
 *
 * An `event_batch` is a run of events the fold applies in order before
 * advancing its cursor past them. A malformed item admits two answers, and this
 * module implements exactly one: the frame is refused with a
 * `BackendClientError('parse', ...)` naming the offending index and field, and
 * the transport that carried it tears the subscription down. Rejection, not
 * dropping, because:
 *
 * - The validators here accept every forward-compatible shape a newer server
 *   can produce (see the unknown-enum policy on `requireString`), so
 *   "malformed" never means "newer than this client". It means the peer
 *   violated the schema it generates this client's types from, which is a
 *   defect in the peer rather than the version skew this boundary exists to
 *   survive.
 * - Dropping an item leaves a hole inside a sequence range the fold then
 *   reports as fully applied: `reduceEventBatch` advances `CoreState.sequence`
 *   to `through_sequence`, so no resume ever asks for the dropped event again.
 *   The hole is permanent, and a counter beside it is a record of the damage,
 *   not a repair.
 * - A drop that did preserve the hole would be re-fetched by
 *   `StreamReconciler`'s backfill, re-dropped, and re-detected: a loop, not a
 *   recovery.
 * - Rejection is already what every other malformed frame in this module gets,
 *   so the boundary has one rule instead of two.
 *
 * The cost, accepted deliberately: one non-conforming event takes the stream
 * down. It comes back as a bounded, reported failure rather than a silently
 * wrong run. `PersistentEventStream` redials on its finite backoff schedule,
 * and once that schedule is spent the disconnect stands with this error's
 * message, which names the item, as its explanation.
 */
function validateEventBatch(record: Record<string, unknown>): void {
  validateEventList(record, 'events', STREAM);
  optionalNumber(record, 'through_sequence', STREAM);
  optionalString(record, 'store_id', STREAM);
  // Kind only, deliberately. That `history_after_sequence` is a non-negative
  // safe integer is `StreamReconciler.declaredFloorOf`'s check and stays
  // there: its comment records why the boundary must not refuse a value the
  // web client currently folds through `?? 0` and never reads back.
  optionalNumber(record, 'history_after_sequence', STREAM);
  validateActiveExecutions(record, STREAM);
}

function validateProtocolError(record: Record<string, unknown>): void {
  nullableString(record, 'request_id', STREAM);
  optionalString(record, 'client_id', STREAM);
  requireString(record, 'code', STREAM);
  requireString(record, 'message', STREAM);
  validateDiagnostic(record, STREAM);
}

function validateEventList(record: Record<string, unknown>, key: string, path: string): void {
  const items = record[key];
  if (!Array.isArray(items)) throw fieldError(path, key, 'an array');
  for (let index = 0; index < items.length; index += 1) {
    validateRunEvent(items[index], `${path} ${key}[${index}]`);
  }
}

function validateRunEvent(value: unknown, path: string): void {
  const record = requireRecord(value, path);
  const version = record['protocol_version'];
  if (version !== undefined && version !== 1) throw fieldError(path, 'protocol_version', '1');
  optionalNumber(record, 'sequence', path);
  optionalString(record, 'run_id', path);
  requireString(record, 'timestamp', path);
  requireString(record, 'type', path);
  optionalString(record, 'text', path);
  nullableString(record, 'status', path);
  nullableString(record, 'round_label', path);
  nullableString(record, 'agent_kind', path);
  nullableString(record, 'invocation_id', path);
  nullableString(record, 'execution_id', path);
  nullableString(record, 'chat_thread_id', path);
  validateDiagnostic(record, path);
  validateEventData(record, path);
}

/**
 * `RunEvent.data` is a tagged union of thirty payloads, and every one of them
 * is open: the generated variants carry `[k: string]: unknown` because the
 * server models accept extra keys. Validated to the depth the tag makes
 * meaningful, so absent, null, or an object carrying a string `kind`.
 * Descending per variant would put a second copy of thirty server models here,
 * and a weak one, since each variant's index signature admits anything it did
 * not name; the folds already branch on `kind` and ignore a payload whose tag
 * they do not recognize.
 */
function validateEventData(record: Record<string, unknown>, path: string): void {
  const data = record['data'];
  if (data === undefined || data === null) return;
  if (!isRecord(data)) throw fieldError(path, 'data', 'an object or null when present');
  requireString(data, 'kind', `${path}.data`);
}

function validateActiveExecutions(record: Record<string, unknown>, path: string): void {
  const items = record['active_executions'];
  if (items === undefined) return;
  if (!Array.isArray(items)) throw fieldError(path, 'active_executions', 'an array when present');
  for (let index = 0; index < items.length; index += 1) {
    validateActiveExecution(items[index], `${path} active_executions[${index}]`);
  }
}

function validateActiveExecution(value: unknown, path: string): void {
  const record = requireRecord(value, path);
  requireString(record, 'execution_id', path);
  requireString(record, 'agent_kind', path);
  requireString(record, 'round_label', path);
  requireString(record, 'stage', path);
  nullableNumber(record, 'attempt', path);
  requireString(record, 'assignment', path);
  requireString(record, 'started_at', path);
  nullableString(record, 'driver', path);
  nullableString(record, 'provider', path);
  nullableString(record, 'model', path);
  const activityPath = `${path}.activity`;
  const activity = requireRecord(record['activity'], activityPath);
  requireString(activity, 'kind', activityPath);
  requireString(activity, 'mode', activityPath);
  requireString(activity, 'summary', activityPath);
  nullableString(activity, 'tool', activityPath);
}

/**
 * The record's `diagnostic`, validated, or null when it carries none. Returns
 * the value so the one caller that needs it as a `Diagnostic` gets it from the
 * function whose body established that it is one.
 */
function validateDiagnostic(record: Record<string, unknown>, path: string): Diagnostic | null {
  const value = record['diagnostic'];
  if (value === undefined || value === null) return null;
  if (!isRecord(value)) throw fieldError(path, 'diagnostic', 'an object or null when present');
  const nested = `${path}.diagnostic`;
  optionalString(value, 'id', nested);
  requireString(value, 'code', nested);
  requireString(value, 'summary', nested);
  nullableString(value, 'detail', nested);
  nullableString(value, 'hint', nested);
  requireString(value, 'scope', nested);
  optionalString(value, 'severity', nested);
  optionalString(value, 'retryability', nested);
  nullableString(value, 'cause_id', nested);
  nullableString(value, 'debug_ref', nested);
  nullableString(value, 'source', nested);
  // Re-read rather than asserting `value`: an index-signature read is `unknown`,
  // so this is one assertion out of the type the checks above established, not a
  // double one through `unknown`.
  return record['diagnostic'] as Diagnostic;
}

function parseJson(line: string, path: string): unknown {
  try {
    return JSON.parse(line);
  } catch (error) {
    throw new BackendClientError(
      'parse',
      `Invalid server ${path} JSON: ${error instanceof Error ? error.message : String(error)}`,
      {cause: error},
    );
  }
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new BackendClientError('parse', `Invalid server ${path}: expected an object`);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Unknown-enum policy: a closed set's kind is checked here, its membership is
 * not.
 *
 * `RunEvent.type`, `RunEvent.status`, `RunEvent.data.kind`,
 * `ActiveAgentExecution.activity.kind`, `ActiveAgentExecution.activity.mode`,
 * `Diagnostic.scope`, `Diagnostic.severity`, and `Diagnostic.retryability` all
 * generate as closed string unions, and every one of them is validated above
 * with `requireString` or `nullableString` rather than against its members.
 * Within one `protocol_version`, adding a member to a set the server emits is a
 * backward-compatible server change, and `protocol_version` is the field that
 * says two peers disagree about the shape of the protocol at all. Refusing an
 * unrecognized member would instead make every stale client unusable against a
 * newer server, which is the skew this boundary exists to survive.
 *
 * The consequence, stated so consumers can rely on it: a closed-set value this
 * package hands out may lie outside its declared union. A `switch` over one of
 * these fields is exhaustive to the compiler and not at runtime, so a consumer
 * keeps a default arm, and that arm has to mean "no information about this
 * field" rather than any particular member.
 *
 * Membership is checked in exactly one place, `parseServerMessage`'s switch on
 * `type`, because that tag selects which `ServerMessage` member is returned, so
 * an unrecognized one has no typed result to return at all. It becomes a parse
 * failure, or a capability refusal by way of `unknownStreamLineError`.
 *
 * The consumer half belongs to the folds, not here, and #873 owns it.
 * `RunSnapshot.status` is one of these fields, and an unrecognized status
 * currently folds as a run that ended and stops the reconnect loop, which is
 * the "read it as a particular member" answer this policy rules out. Nothing
 * about that behavior changes here.
 */
function requireString(record: Record<string, unknown>, key: string, path: string): void {
  if (typeof record[key] !== 'string') throw fieldError(path, key, 'a string');
}

function optionalString(record: Record<string, unknown>, key: string, path: string): void {
  const value = record[key];
  if (value !== undefined && typeof value !== 'string') {
    throw fieldError(path, key, 'a string when present');
  }
}

function nullableString(record: Record<string, unknown>, key: string, path: string): void {
  const value = record[key];
  if (value !== undefined && value !== null && typeof value !== 'string') {
    throw fieldError(path, key, 'a string or null when present');
  }
}

function requireNumber(record: Record<string, unknown>, key: string, path: string): void {
  if (typeof record[key] !== 'number') throw fieldError(path, key, 'a number');
}

function optionalNumber(record: Record<string, unknown>, key: string, path: string): void {
  const value = record[key];
  if (value !== undefined && typeof value !== 'number') {
    throw fieldError(path, key, 'a number when present');
  }
}

function nullableNumber(record: Record<string, unknown>, key: string, path: string): void {
  const value = record[key];
  if (value !== undefined && value !== null && typeof value !== 'number') {
    throw fieldError(path, key, 'a number or null when present');
  }
}

function requireBoolean(record: Record<string, unknown>, key: string, path: string): void {
  if (typeof record[key] !== 'boolean') throw fieldError(path, key, 'a boolean');
}

function fieldError(path: string, key: string, expected: string): BackendClientError {
  return new BackendClientError('parse', `Invalid server ${path}: ${key} must be ${expected}`);
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
