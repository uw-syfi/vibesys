import {BackendClientError, ServerError} from './errors.js';
import responsePayloadSchema from './generated/response-payload.schema.js';
import runEventSchema from './generated/run-event.schema.js';
import type {Diagnostic, ProtocolResponse, RunEvent, ServerMessage} from './protocol.js';

const RESPONSE = 'response';
const STREAM = 'event-stream message';

/**
 * Parse and validate a response independently of the transport framing.
 *
 * A compact generated descriptor validates every response payload before the
 * assertion. The hand-written checks retain their specific diagnostics for the
 * envelope, event batch, and diagnostic that have public error-taxonomy behavior.
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
  validateSchema(value, responsePayloadSchema as Schema, RESPONSE);
  return value as ProtocolResponse;
}

/** Validate an event from a non-transport boundary and return its typed value. */
export function validateRunEvent(value: unknown): RunEvent {
  return validateRunEventAt(value, 'run event');
}

type Schema = Record<string, unknown>;
const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|([+-])(\d{2}):(\d{2}))$/;
// RFC 3339 admits :60 only at an announced UTC leap second. These are every
// insertion month through the most recent leap second in December 2016.
const RFC3339_LEAP_SECONDS = new Set([
  '1972-06',
  '1972-12',
  '1973-12',
  '1974-12',
  '1975-12',
  '1976-12',
  '1977-12',
  '1978-12',
  '1979-12',
  '1981-06',
  '1982-06',
  '1983-06',
  '1985-06',
  '1987-12',
  '1989-12',
  '1990-12',
  '1992-06',
  '1993-06',
  '1994-06',
  '1995-12',
  '1997-06',
  '1998-12',
  '2005-12',
  '2008-12',
  '2012-06',
  '2015-06',
  '2016-12',
]);

/** Validate generated protocol shape, intentionally leaving closed-set membership open. */
function validateSchema(value: unknown, schema: Schema, path: string): void {
  const ref = schema['$ref'];
  if (typeof ref === 'string') {
    validateSchema(value, schemaReference(ref), path);
    return;
  }
  const constant = schema['const'];
  if (constant !== undefined) {
    validateConstant(value, constant, path);
    return;
  }
  const choices = schema['anyOf'];
  if (Array.isArray(choices)) {
    validateAnyOf(value, choices, path);
    return;
  }
  const alternatives = schema['oneOf'];
  const discriminator = schema['discriminator'];
  if (Array.isArray(alternatives) && isRecord(discriminator)) {
    validateTaggedUnion(value, discriminator, path);
    return;
  }
  validateTypedSchema(value, schema, path);
}

function schemaDefinition(name: string): Schema {
  for (const document of [responsePayloadSchema, runEventSchema]) {
    const definitions = (document as {$defs?: Record<string, Schema>}).$defs;
    const definition = definitions?.[name];
    if (definition !== undefined) return definition;
  }
  throw new BackendClientError('parse', `Invalid protocol schema ref: #/$defs/${name}`);
}

function schemaReference(ref: string): Schema {
  const name = ref.startsWith('#/$defs/') ? ref.slice('#/$defs/'.length) : ref;
  return schemaDefinition(name);
}

function validateConstant(value: unknown, constant: unknown, path: string): void {
  // String literals are closed-set members, which stay forward compatible.
  // Numeric literals are protocol-version boundaries and must agree exactly.
  if (typeof constant === 'string' && typeof value === 'string') return;
  if (value === constant) return;
  throw new BackendClientError('parse', `Invalid server ${path}: must be a ${typeof constant}`);
}

function validateAnyOf(value: unknown, choices: unknown[], path: string): void {
  let mismatch: BackendClientError | undefined;
  for (const choice of choices) {
    if (!isRecord(choice)) continue;
    try {
      validateSchema(value, choice, path);
      return;
    } catch (error) {
      if (!(error instanceof BackendClientError)) throw error;
      mismatch ??= error;
    }
  }
  throw (
    mismatch ??
    new BackendClientError('parse', `Invalid server ${path}: does not match its protocol shape`)
  );
}

function validateTaggedUnion(
  value: unknown,
  discriminator: Record<string, unknown>,
  path: string,
): void {
  const record = requireRecord(value, path);
  const property = discriminator['propertyName'];
  const mapping = discriminator['mapping'];
  if (typeof property !== 'string' || !isRecord(mapping)) {
    throw new BackendClientError('parse', `Invalid protocol schema discriminator at ${path}`);
  }
  const tag = record[property];
  if (typeof tag !== 'string') throw fieldError(path, property, 'a string');
  const reference = mapping[tag];
  // #869: a newer server may add a union member without changing the protocol
  // version. Its tag and object shape are still checked, but only a member in
  // this generated client's mapping has a known schema to validate deeply.
  if (typeof reference !== 'string') return;
  validateSchema(value, {$ref: reference}, path);
}

function validateTypedSchema(value: unknown, schema: Schema, path: string): void {
  const type = schema['type'];
  switch (type) {
    case 'array':
      validateArray(value, schema, path);
      return;
    case 'object':
      validateObject(value, schema, path);
      return;
    case 'null':
      validatePrimitive(value === null, type, path);
      return;
    case 'string':
      validateString(value, schema, path);
      return;
    case 'number':
      validateNumber(value, schema, path, false);
      return;
    case 'integer':
      validateNumber(value, schema, path, true);
      return;
    case 'boolean':
      validatePrimitive(typeof value === 'boolean', type, path);
  }
}

function validateString(value: unknown, schema: Schema, path: string): void {
  if (typeof value !== 'string') {
    validatePrimitive(false, 'string', path);
    return;
  }
  if (schema['format'] !== 'date-time') return;
  if (!isRfc3339DateTime(value)) {
    throw new BackendClientError('parse', `Invalid server ${path}: must be a date-time`);
  }
}

function isRfc3339DateTime(value: string): boolean {
  const parts = RFC3339_DATE_TIME.exec(value);
  if (parts === null) return false;
  const [
    ,
    yearText,
    monthText,
    dayText,
    hourText,
    minuteText,
    secondText,
    offsetSign,
    offsetHourText,
    offsetMinuteText,
  ] = parts;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const offsetHour = offsetHourText === undefined ? 0 : Number(offsetHourText);
  const offsetMinute = offsetMinuteText === undefined ? 0 : Number(offsetMinuteText);
  return (
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth(year, month) &&
    hour <= 23 &&
    minute <= 59 &&
    offsetHour <= 23 &&
    offsetMinute <= 59 &&
    (second <= 59 ||
      (second === 60 &&
        isAnnouncedLeapSecond({
          year,
          month,
          day,
          hour,
          minute,
          offsetSign,
          offsetHour,
          offsetMinute,
        })))
  );
}

interface DateTimeParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  offsetSign: string | undefined;
  offsetHour: number;
  offsetMinute: number;
}

function isAnnouncedLeapSecond({
  year,
  month,
  day,
  hour,
  minute,
  offsetSign,
  offsetHour,
  offsetMinute,
}: DateTimeParts): boolean {
  const direction = offsetSign === '-' ? -1 : 1;
  const offsetMilliseconds = direction * (offsetHour * 60 + offsetMinute) * 60_000;
  // Date.UTC treats years 0 through 99 as 1900 through 1999. Set the full year
  // explicitly so an ancient date cannot borrow a modern leap-second entry.
  const utc = new Date(0);
  utc.setUTCFullYear(year, month - 1, day);
  utc.setUTCHours(hour, minute, 59, 0);
  utc.setTime(utc.getTime() - offsetMilliseconds);
  const utcMonth = `${utc.getUTCFullYear()}-${String(utc.getUTCMonth() + 1).padStart(2, '0')}`;
  return (
    utc.getUTCDate() === daysInMonth(utc.getUTCFullYear(), utc.getUTCMonth() + 1) &&
    utc.getUTCHours() === 23 &&
    utc.getUTCMinutes() === 59 &&
    RFC3339_LEAP_SECONDS.has(utcMonth)
  );
}

function daysInMonth(year: number, month: number): number {
  if (month === 2) return isLeapYear(year) ? 29 : 28;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

function validateNumber(value: unknown, schema: Schema, path: string, integer: boolean): void {
  const type = integer ? 'integer' : 'number';
  if (typeof value !== 'number' || (integer && !Number.isInteger(value))) {
    validatePrimitive(false, type, path);
    return;
  }
  const minimum = schema['minimum'];
  if (typeof minimum === 'number' && value < minimum) {
    throw new BackendClientError('parse', `Invalid server ${path}: must be at least ${minimum}`);
  }
}

function validatePrimitive(valid: boolean, type: string, path: string): void {
  if (valid) return;
  throw new BackendClientError('parse', `Invalid server ${path}: must be a ${type}`);
}

function validateArray(value: unknown, schema: Schema, path: string): void {
  if (!Array.isArray(value))
    throw new BackendClientError('parse', `Invalid server ${path}: must be an array`);
  const items = schema['items'];
  if (!isRecord(items)) return;
  for (const [index, item] of value.entries()) validateSchema(item, items, `${path}[${index}]`);
}

function validateObject(value: unknown, schema: Schema, path: string): void {
  const record = requireRecord(value, path);
  validateRequired(record, schema['required'], path);
  const properties = schema['properties'];
  if (!isRecord(properties)) return;
  // #869: newly added server fields must not make an older client disconnect.
  // Validate known fields only; the generated types retain the known contract.
  for (const [key, child] of Object.entries(properties)) {
    if (key in record && isRecord(child)) validateSchema(record[key], child, `${path}.${key}`);
  }
}

function validateRequired(record: Record<string, unknown>, required: unknown, path: string): void {
  if (!Array.isArray(required)) return;
  for (const key of required) {
    if (typeof key === 'string' && !(key in record)) throw fieldError(path, key, 'present');
  }
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
      validateRunEventAt(record['event'], `${STREAM} event`);
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
  optionalBoolean(record, 'rebootstrap', STREAM);
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
    validateRunEventAt(items[index], `${path} ${key}[${index}]`);
  }
}

function validateRunEventAt(value: unknown, path: string): RunEvent {
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
  validateSchema(value, runEventSchema as Schema, path);
  return value as RunEvent;
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
  validateValidationPaths(value, nested);
  // Re-read rather than asserting `value`: an index-signature read is `unknown`,
  // so this is one assertion out of the type the checks above established, not a
  // double one through `unknown`.
  return record['diagnostic'] as Diagnostic;
}

function validateValidationPaths(record: Record<string, unknown>, path: string): void {
  const paths = record['validation_paths'];
  if (paths === undefined || paths === null) return;
  if (!Array.isArray(paths))
    throw fieldError(path, 'validation_paths', 'an array or null when present');
  for (let index = 0; index < paths.length; index += 1) {
    const segments = paths[index];
    const segmentPath = `validation_paths[${index}]`;
    if (!Array.isArray(segments)) throw fieldError(path, segmentPath, 'an array');
    for (let segmentIndex = 0; segmentIndex < segments.length; segmentIndex += 1) {
      const segment = segments[segmentIndex];
      if (typeof segment !== 'string' && typeof segment !== 'number') {
        throw fieldError(path, `${segmentPath}[${segmentIndex}]`, 'a string or number');
      }
    }
  }
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

function optionalBoolean(record: Record<string, unknown>, key: string, path: string): void {
  const value = record[key];
  if (value !== undefined && typeof value !== 'boolean') {
    throw fieldError(path, key, 'a boolean when present');
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
