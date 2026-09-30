import {BackendClientError, ServerError} from './errors.js';
import type {Diagnostic, ProtocolResponse, ServerMessage} from './protocol.js';

/** Parse and validate a response independently of the transport framing. */
export function parseProtocolResponse(line: string): ProtocolResponse {
  const value = parseRecord(line, 'response');
  if (value['protocol_version'] !== 1) {
    throw new BackendClientError('parse', 'Unsupported server protocol version');
  }
  if (typeof value['request_id'] !== 'string') {
    throw new BackendClientError('parse', 'Invalid server response: request_id must be a string');
  }
  if (typeof value['ok'] !== 'boolean') {
    throw new BackendClientError('parse', 'Invalid server response: ok must be a boolean');
  }
  validateClientId(value, 'response');
  return value as unknown as ProtocolResponse;
}

/** Parse and validate a streamed server message independently of framing. */
export function parseServerMessage(line: string): ServerMessage {
  const value = parseRecord(line, 'event-stream message');
  const type = value['type'];
  if (type === 'subscribed') {
    if (
      typeof value['request_id'] !== 'string' ||
      typeof value['run_id'] !== 'string' ||
      typeof value['latest_sequence'] !== 'number'
    ) {
      throw new BackendClientError('parse', 'Invalid subscribed message');
    }
    validateClientId(value, 'subscribed message');
  } else if (type === 'event') {
    if (!isRecord(value['event'])) throw new BackendClientError('parse', 'Invalid event message');
  } else if (type === 'event_batch') {
    if (!Array.isArray(value['events'])) {
      throw new BackendClientError('parse', 'Invalid event batch message');
    }
    validateHistoryFloor(value);
  } else if (type === 'protocol_error') {
    if (typeof value['code'] !== 'string' || typeof value['message'] !== 'string') {
      throw new BackendClientError('parse', 'Invalid protocol error message');
    }
    validateClientId(value, 'protocol error message');
  } else {
    throw unknownStreamLineError(value);
  }
  return value as unknown as ServerMessage;
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
    isRecord(value['diagnostic']) ? (value['diagnostic'] as unknown as Diagnostic) : null,
  );
}

function parseRecord(line: string, description: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new BackendClientError(
      'parse',
      `Invalid server ${description} JSON: ${error instanceof Error ? error.message : String(error)}`,
      {cause: error},
    );
  }
  if (!isRecord(value)) {
    throw new BackendClientError('parse', `Invalid server ${description}: expected an object`);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Every consumer uses `history_after_sequence` as a sequence: the floor to
 * record against the fold, and the top of the next `query.events` range. The
 * protocol model bounds it (`int = Field(default=0, ge=0)`,
 * `src/server/api/protocol.py:569`), but the wire carries JSON, where a
 * negative or fractional number is representable and only the `events` array
 * was checked above. A floor no range can express has to be refused here,
 * where the rest of the frame is validated, rather than in each consumer:
 * `StreamReconciler` and `clients/web/src/store.ts` both read the field, and
 * a check in one of them leaves the other unguarded.
 */
function validateHistoryFloor(value: Record<string, unknown>): void {
  const floor = value['history_after_sequence'];
  if (floor === undefined) return;
  if (typeof floor !== 'number' || !Number.isSafeInteger(floor) || floor < 0) {
    throw new BackendClientError(
      'parse',
      `Invalid event batch message: history_after_sequence must be a non-negative integer, received ${String(floor)}`,
    );
  }
}

function validateClientId(value: Record<string, unknown>, description: string): void {
  if (value['client_id'] !== undefined && typeof value['client_id'] !== 'string') {
    throw new BackendClientError('parse', `Invalid ${description}: client_id must be a string`);
  }
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
